"""Node.js (JavaScript) adapter.

Prefers tree-sitter for parsing; falls back to regex if the grammar is missing.
Extracts: imports (CommonJS ``require`` + ES ``import``), AWS SDK v3 client
instantiations (``new SQSClient(...)``), and SDK method calls.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from maf_generic_migrator_v1.platform_core.ir import Import, ServiceCall, UnitIR

from ._kg_builder import build_kg_from_ir
from ._tree_sitter_utils import find_nodes_by_type, line_of, node_text, parse
from .base import LanguageAdapter

if TYPE_CHECKING:
    from maf_generic_migrator_v1.platform_core.kg import KGStore

# --- Regex fallback (used when tree-sitter unavailable) ------------------- #
_REQUIRE = re.compile(r"""(?m)^\s*(?:const|let|var)\s+(?:\{[^}]+\}|\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")
_ES_IMPORT = re.compile(r"""(?m)^\s*import\s+[^'"]+\s+from\s+['"]([^'"]+)['"]""")
_AWS_CLIENT = re.compile(r"""new\s+(\w+Client)\s*\(""")


class NodeAdapter(LanguageAdapter):
    language = "node"
    supported_suffixes = (".js", ".mjs", ".cjs")

    def extract_unit(self, repo_root: Path, unit_root: Path) -> UnitIR:
        files = self.files_in_unit(unit_root)
        imports: list[Import] = []
        service_calls: list[ServiceCall] = []
        loc = 0
        byte_size = 0

        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            byte_size += len(text.encode("utf-8"))
            loc += text.count("\n") + 1
            rel = str(f.relative_to(repo_root))

            root, src_bytes = parse("node", text)
            if root is None:
                self._regex_extract(text, rel, imports, service_calls)
                continue

            self._ts_extract(root, src_bytes, rel, imports, service_calls)

        return UnitIR(
            unit_id=self.unit_id_for(repo_root, unit_root),
            kind="module",
            language="node",
            root_path=str(unit_root.relative_to(repo_root)),
            handler_entry=_guess_handler_entry(files),
            files=[str(f.relative_to(repo_root)) for f in files],
            imports=imports,
            service_calls=service_calls,
            loc=loc,
            byte_size=byte_size,
        )

    def extract_kg(self, repo_root: Path, unit_root: Path, store: "KGStore") -> None:
        ir = self.extract_unit(repo_root, unit_root)
        build_kg_from_ir(ir, store, resolve_call=_default_aws_resolver())

    # --- tree-sitter path ------------------------------------------------- #

    def _ts_extract(
        self,
        root,  # noqa: ANN001
        src: bytes,
        rel: str,
        imports: list[Import],
        service_calls: list[ServiceCall],
    ) -> None:
        # ES import statements: source node is a string literal.
        for imp_node in find_nodes_by_type(root, {"import_statement"}):
            source_node = imp_node.child_by_field_name("source")
            if source_node is None:
                continue
            module = _unquote(node_text(source_node, src))
            imports.append(
                Import(
                    module=module,
                    file=rel,
                    line=line_of(imp_node),
                    is_external=_looks_external(module),
                )
            )

        # CommonJS: require("...") — a call_expression whose callee is 'require'.
        for call in find_nodes_by_type(root, {"call_expression"}):
            callee = call.child_by_field_name("function")
            args = call.child_by_field_name("arguments")
            if callee is not None and args is not None:
                if node_text(callee, src) == "require" and args.named_child_count >= 1:
                    arg0 = args.named_children[0]
                    if arg0.type in {"string", "string_fragment"}:
                        module = _unquote(node_text(arg0, src))
                        imports.append(
                            Import(
                                module=module,
                                file=rel,
                                line=line_of(call),
                                is_external=_looks_external(module),
                            )
                        )

        # AWS SDK v3 client instantiations: new <Something>Client(...)
        for new_expr in find_nodes_by_type(root, {"new_expression"}):
            ctor = new_expr.child_by_field_name("constructor")
            if ctor is None:
                continue
            name = node_text(ctor, src)
            if name.endswith("Client"):
                service_calls.append(
                    ServiceCall(
                        service=_client_to_service(name),
                        operation="new",
                        file=rel,
                        line=line_of(new_expr),
                    )
                )

    # --- regex fallback --------------------------------------------------- #

    def _regex_extract(
        self,
        text: str,
        rel: str,
        imports: list[Import],
        service_calls: list[ServiceCall],
    ) -> None:
        for m in _REQUIRE.finditer(text):
            imports.append(
                Import(
                    module=m.group(1),
                    file=rel,
                    line=text.count("\n", 0, m.start()) + 1,
                    is_external=_looks_external(m.group(1)),
                )
            )
        for m in _ES_IMPORT.finditer(text):
            imports.append(
                Import(
                    module=m.group(1),
                    file=rel,
                    line=text.count("\n", 0, m.start()) + 1,
                    is_external=_looks_external(m.group(1)),
                )
            )
        for m in _AWS_CLIENT.finditer(text):
            service_calls.append(
                ServiceCall(
                    service=_client_to_service(m.group(1)),
                    operation="new",
                    file=rel,
                    line=text.count("\n", 0, m.start()) + 1,
                )
            )


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] in "\"'`" and raw[0] == raw[-1]:
        return raw[1:-1]
    return raw


def _looks_external(module: str) -> bool:
    return not (module.startswith(".") or module.startswith("/"))


def _client_to_service(client_class: str) -> str:
    mapping = {
        "SQSClient": "sqs",
        "SNSClient": "sns",
        "S3Client": "s3",
        "DynamoDBClient": "dynamodb",
        "DynamoDBDocumentClient": "dynamodb",
        "SFNClient": "stepfunctions",
        "EventBridgeClient": "eventbridge",
        "SecretsManagerClient": "secretsmanager",
        "KinesisClient": "kinesis",
        "LambdaClient": "lambda",
        "SSMClient": "ssm",
        "CognitoIdentityProviderClient": "cognito",
    }
    return mapping.get(client_class, client_class.replace("Client", "").lower())


_HANDLER_FILENAMES = ("index.js", "handler.js", "main.js", "lambda.js", "function.js")


def _guess_handler_entry(files: list[Path]) -> str | None:
    for name in _HANDLER_FILENAMES:
        for f in files:
            if f.name == name:
                return f"{f.name}:handler"
    return None

# Re-exports for TypeScript adapter reuse.
_AWS_SDK_CLIENT = _AWS_CLIENT


def _default_aws_resolver():
    """Lazy import of the AWS cartridge's SDK resolver (see python.py)."""
    try:
        from maf_generic_migrator_v1.cartridges.aws_lambda_polyglot_to_azure_fn_py.aws_sdk_patterns import (
            resolve,
        )
    except Exception:  # noqa: BLE001
        return None
    return resolve
