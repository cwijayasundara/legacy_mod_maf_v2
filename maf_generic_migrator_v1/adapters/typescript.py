"""TypeScript adapter. Tree-sitter primary; regex fallback."""
from __future__ import annotations

import re
from pathlib import Path

from maf_generic_migrator_v1.platform_core.ir import Import, ServiceCall, UnitIR

from ._tree_sitter_utils import find_nodes_by_type, line_of, node_text, parse
from .base import LanguageAdapter
from .node import _AWS_SDK_CLIENT, _client_to_service, _looks_external

_TS_IMPORT = re.compile(r"""(?m)^\s*import\s+[^;]+\s+from\s+['"]([^'"]+)['"]""")


class TypeScriptAdapter(LanguageAdapter):
    language = "typescript"
    supported_suffixes = (".ts", ".tsx")

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

            root, src_bytes = parse("typescript", text)
            if root is None:
                self._regex_extract(text, rel, imports, service_calls)
                continue
            self._ts_extract(root, src_bytes, rel, imports, service_calls)

        return UnitIR(
            unit_id=self.unit_id_for(repo_root, unit_root),
            kind="module",
            language="typescript",
            root_path=str(unit_root.relative_to(repo_root)),
            handler_entry=_guess_handler_entry(files),
            files=[str(f.relative_to(repo_root)) for f in files],
            imports=imports,
            service_calls=service_calls,
            loc=loc,
            byte_size=byte_size,
        )

    def _ts_extract(self, root, src, rel, imports, service_calls) -> None:  # noqa: ANN001
        for imp_node in find_nodes_by_type(root, {"import_statement"}):
            source_node = imp_node.child_by_field_name("source")
            if source_node is None:
                continue
            module = _unquote(node_text(source_node, src))
            imports.append(
                Import(module=module, file=rel, line=line_of(imp_node), is_external=_looks_external(module))
            )
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

    def _regex_extract(self, text: str, rel: str, imports, service_calls) -> None:
        for m in _TS_IMPORT.finditer(text):
            imports.append(
                Import(
                    module=m.group(1),
                    file=rel,
                    line=text.count("\n", 0, m.start()) + 1,
                    is_external=_looks_external(m.group(1)),
                )
            )
        for m in _AWS_SDK_CLIENT.finditer(text):
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


_HANDLER_FILENAMES = ("index.ts", "handler.ts", "main.ts", "lambda.ts")


def _guess_handler_entry(files: list[Path]) -> str | None:
    for name in _HANDLER_FILENAMES:
        for f in files:
            if f.name == name:
                return f"{f.name}:handler"
    return None
