"""Java adapter. Tree-sitter primary; regex fallback."""
from __future__ import annotations

import re
from pathlib import Path

from maf_generic_migrator_v1.platform_core.ir import Import, ServiceCall, UnitIR

from ._tree_sitter_utils import find_nodes_by_type, line_of, node_text, parse
from .base import LanguageAdapter

_IMPORT_RX = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.MULTILINE)
_AWS_SDK_BUILDER_RX = re.compile(r"""(\w+Client)\.builder\(\)""")
_AWS_SDK_CONSTRUCT_RX = re.compile(r"""new\s+(\w+Client)\s*\(""")


class JavaAdapter(LanguageAdapter):
    language = "java"
    supported_suffixes = (".java",)

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

            root, src = parse("java", text)
            if root is None:
                self._regex_extract(text, rel, imports, service_calls)
                continue
            self._ts_extract(root, src, rel, imports, service_calls)

        return UnitIR(
            unit_id=self.unit_id_for(repo_root, unit_root),
            kind="module",
            language="java",
            root_path=str(unit_root.relative_to(repo_root)),
            handler_entry=_guess_handler_entry(files),
            files=[str(f.relative_to(repo_root)) for f in files],
            imports=imports,
            service_calls=service_calls,
            loc=loc,
            byte_size=byte_size,
        )

    def _ts_extract(self, root, src: bytes, rel, imports, service_calls) -> None:  # noqa: ANN001
        # Imports
        for imp in find_nodes_by_type(root, {"import_declaration"}):
            # grab the identifier / scoped_identifier / asterisk inside
            name_text = node_text(imp, src)
            m = re.search(r"import\s+(?:static\s+)?([\w.*]+)\s*;", name_text)
            if m:
                module = m.group(1)
                imports.append(
                    Import(module=module, file=rel, line=line_of(imp), is_external=_looks_external(module))
                )

        # ClientName.builder() pattern
        for call in find_nodes_by_type(root, {"method_invocation"}):
            name_node = call.child_by_field_name("name")
            obj_node = call.child_by_field_name("object")
            if name_node is not None and node_text(name_node, src) == "builder" and obj_node is not None:
                obj_text = node_text(obj_node, src)
                if obj_text.endswith("Client"):
                    service_calls.append(
                        ServiceCall(
                            service=_client_to_service(obj_text),
                            operation="builder",
                            file=rel,
                            line=line_of(call),
                        )
                    )

        # new <Client>(...) pattern
        for oc in find_nodes_by_type(root, {"object_creation_expression"}):
            type_node = oc.child_by_field_name("type")
            if type_node is None:
                continue
            type_text = node_text(type_node, src)
            if type_text.endswith("Client"):
                service_calls.append(
                    ServiceCall(
                        service=_client_to_service(type_text),
                        operation="new",
                        file=rel,
                        line=line_of(oc),
                    )
                )

    def _regex_extract(self, text, rel, imports, service_calls) -> None:
        for m in _IMPORT_RX.finditer(text):
            mod = m.group(1)
            imports.append(
                Import(module=mod, file=rel, line=text.count("\n", 0, m.start()) + 1, is_external=_looks_external(mod))
            )
        for rx in (_AWS_SDK_BUILDER_RX, _AWS_SDK_CONSTRUCT_RX):
            for m in rx.finditer(text):
                service_calls.append(
                    ServiceCall(
                        service=_client_to_service(m.group(1)),
                        operation="new",
                        file=rel,
                        line=text.count("\n", 0, m.start()) + 1,
                    )
                )


def _looks_external(module: str) -> bool:
    return module.startswith(("java.", "javax.", "jakarta.", "org.", "com.amazonaws.", "software.amazon.", "com.google."))


def _client_to_service(client_class: str) -> str:
    mapping = {
        "SqsClient": "sqs", "SnsClient": "sns", "S3Client": "s3",
        "DynamoDbClient": "dynamodb", "DynamoDBClient": "dynamodb",
        "SfnClient": "stepfunctions", "EventBridgeClient": "eventbridge",
        "SecretsManagerClient": "secretsmanager", "KinesisClient": "kinesis",
        "LambdaClient": "lambda",
        "AmazonSQSClient": "sqs", "AmazonSNSClient": "sns",
        "AmazonS3Client": "s3", "AmazonDynamoDBClient": "dynamodb",
    }
    return mapping.get(client_class, client_class.replace("Client", "").lower())


def _guess_handler_entry(files: list[Path]) -> str | None:
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if "RequestHandler" in text or "RequestStreamHandler" in text:
            return f"{f.name}:handleRequest"
    return None
