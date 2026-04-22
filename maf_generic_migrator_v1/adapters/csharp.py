"""C# / .NET adapter. Tree-sitter primary; regex fallback."""
from __future__ import annotations

import re
from pathlib import Path

from maf_generic_migrator_v1.platform_core.ir import Import, ServiceCall, UnitIR

from ._tree_sitter_utils import find_nodes_by_type, line_of, node_text, parse
from .base import LanguageAdapter

_USING_RX = re.compile(r"^\s*using\s+([\w.]+)\s*;", re.MULTILINE)
_AWS_NEW_RX = re.compile(r"""new\s+(Amazon\w+Client)\s*\(""")


class CSharpAdapter(LanguageAdapter):
    language = "csharp"
    supported_suffixes = (".cs",)

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

            root, src = parse("csharp", text)
            if root is None:
                self._regex_extract(text, rel, imports, service_calls)
                continue
            self._ts_extract(root, src, rel, imports, service_calls)

        return UnitIR(
            unit_id=self.unit_id_for(repo_root, unit_root),
            kind="module",
            language="csharp",
            root_path=str(unit_root.relative_to(repo_root)),
            handler_entry=_guess_handler_entry(files),
            files=[str(f.relative_to(repo_root)) for f in files],
            imports=imports,
            service_calls=service_calls,
            loc=loc,
            byte_size=byte_size,
        )

    def _ts_extract(self, root, src: bytes, rel, imports, service_calls) -> None:  # noqa: ANN001
        for using in find_nodes_by_type(root, {"using_directive"}):
            name_text = node_text(using, src)
            m = re.search(r"using\s+(?:static\s+)?([\w.]+)", name_text)
            if m:
                mod = m.group(1)
                imports.append(
                    Import(module=mod, file=rel, line=line_of(using), is_external=_looks_external(mod))
                )
        for oc in find_nodes_by_type(root, {"object_creation_expression"}):
            type_node = oc.child_by_field_name("type")
            if type_node is None:
                continue
            type_text = node_text(type_node, src)
            if type_text.startswith("Amazon") and type_text.endswith("Client"):
                service_calls.append(
                    ServiceCall(
                        service=_client_to_service(type_text),
                        operation="new",
                        file=rel,
                        line=line_of(oc),
                    )
                )

    def _regex_extract(self, text, rel, imports, service_calls) -> None:
        for m in _USING_RX.finditer(text):
            mod = m.group(1)
            imports.append(
                Import(module=mod, file=rel, line=text.count("\n", 0, m.start()) + 1, is_external=_looks_external(mod))
            )
        for m in _AWS_NEW_RX.finditer(text):
            service_calls.append(
                ServiceCall(
                    service=_client_to_service(m.group(1)),
                    operation="new",
                    file=rel,
                    line=text.count("\n", 0, m.start()) + 1,
                )
            )


def _looks_external(module: str) -> bool:
    return module.startswith(("System.", "Microsoft.", "Amazon.", "AWSSDK.", "Newtonsoft."))


def _client_to_service(client_class: str) -> str:
    mapping = {
        "AmazonSQSClient": "sqs",
        "AmazonSimpleNotificationServiceClient": "sns",
        "AmazonS3Client": "s3",
        "AmazonDynamoDBClient": "dynamodb",
        "AmazonStepFunctionsClient": "stepfunctions",
        "AmazonEventBridgeClient": "eventbridge",
        "AmazonSecretsManagerClient": "secretsmanager",
        "AmazonKinesisClient": "kinesis",
        "AmazonLambdaClient": "lambda",
    }
    return mapping.get(client_class, client_class.lower())


def _guess_handler_entry(files: list[Path]) -> str | None:
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if "FunctionHandler" in text or "LambdaFunction" in text or "Amazon.Lambda.Core" in text:
            return f"{f.name}:FunctionHandler"
    return None
