"""Python adapter (ported + extended from legacy_mod_maf_v1).

Uses stdlib ``ast`` — zero external dependencies and handles every
syntactically valid Python 3 file. The boto3 call resolver propagates name
bindings so that ``table = ddb.Table('X'); table.put_item(...)`` resolves to
(dynamodb, put_item, 'X').
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from maf_generic_migrator_v1.platform_core.ir import Import, ServiceCall, UnitIR

from ._kg_builder import build_kg_from_ir, python_function_paragraphs
from .base import LanguageAdapter

if TYPE_CHECKING:
    from maf_generic_migrator_v1.platform_core.kg import KGStore

_BOTO3_FACTORIES = {"client", "resource"}
_NAME_KWARGS = {
    "TableName": "dynamodb",
    "Bucket": "s3",
    "QueueUrl": "sqs",
    "TopicArn": "sns",
    "StreamName": "kinesis",
    "SecretId": "secretsmanager",
    "FunctionName": "lambda",
}


class PythonAdapter(LanguageAdapter):
    language = "python"
    supported_suffixes = (".py",)

    def extract_unit(self, repo_root: Path, unit_root: Path) -> UnitIR:
        files = self.files_in_unit(unit_root)
        # When the unit is a single handler file, follow its imports to
        # include the local-package files it depends on (e.g. services/
        # helpers shared across handlers). This mirrors v1's behavior of
        # shipping each migrated module with its dependencies, and keeps
        # each per-file unit's translator prompt self-contained.
        if unit_root.is_file():
            files = self._walk_local_imports(repo_root, files)

        imports: list[Import] = []
        service_calls: list[ServiceCall] = []
        loc = 0
        byte_size = 0

        for f in files:
            src = f.read_text(encoding="utf-8", errors="replace")
            byte_size += len(src.encode("utf-8"))
            loc += src.count("\n") + 1
            rel = str(f.relative_to(repo_root))
            try:
                tree = ast.parse(src, filename=str(f))
            except SyntaxError:
                continue
            imports.extend(self._parse_imports(tree, rel))
            service_calls.extend(self._parse_service_calls(tree, rel))

        return UnitIR(
            unit_id=self.unit_id_for(repo_root, unit_root),
            kind="module",
            language="python",
            root_path=str(unit_root.relative_to(repo_root)),
            handler_entry=_guess_handler_entry(files),
            files=[str(f.relative_to(repo_root)) for f in files],
            imports=imports,
            service_calls=service_calls,
            loc=loc,
            byte_size=byte_size,
        )

    # ----------------------------------------------------------------- #
    # KG path — projects the IR into program / external_call / dataset_ref
    # nodes so the cartridge can run comprehension + render a business
    # spec for the translator prompt. Paragraphs = top-level functions.
    # ----------------------------------------------------------------- #

    def extract_kg(self, repo_root: Path, unit_root: Path, store: "KGStore") -> None:
        ir = self.extract_unit(repo_root, unit_root)
        build_kg_from_ir(
            ir,
            store,
            resolve_call=_default_aws_resolver(),
            function_paragraphs=python_function_paragraphs(repo_root, ir),
        )

    # ----------------------------------------------------------------- #
    # Local-import resolution (used when unit_root is a single file)
    # ----------------------------------------------------------------- #

    def _walk_local_imports(self, repo_root: Path, seed: list[Path]) -> list[Path]:
        """BFS from ``seed`` following imports that resolve to other repo files.

        Returns ``seed`` plus every transitively-reachable in-repo Python
        file. External packages (boto3, azure.*, std-lib) are ignored.
        """
        # Build importable-name -> path map for the repo.
        repo_modules: dict[str, Path] = {}
        for py in repo_root.rglob("*.py"):
            if any(part.startswith(".") or part in {"__pycache__", "node_modules", ".venv"} for part in py.parts):
                continue
            try:
                rel = py.relative_to(repo_root)
            except ValueError:
                continue
            mod = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
            mod = mod.removesuffix(".__init__")
            repo_modules[mod] = py

        visited: set[Path] = set()
        queue: list[Path] = list(seed)
        while queue:
            cur = queue.pop()
            if cur in visited:
                continue
            visited.add(cur)
            try:
                tree = ast.parse(cur.read_text(encoding="utf-8", errors="replace"), filename=str(cur))
            except (SyntaxError, OSError):
                continue
            for mod_name in self._iter_imported_modules(tree, cur, repo_root):
                hit = self._resolve_module(mod_name, repo_modules)
                if hit is not None and hit not in visited:
                    queue.append(hit)
        # Stable order: seed first, then deps alphabetically.
        seed_set = set(seed)
        deps = sorted(p for p in visited if p not in seed_set)
        return list(seed) + deps

    @staticmethod
    def _iter_imported_modules(tree: ast.AST, cur_file: Path, repo_root: Path):
        """Yield dotted module names referenced by import statements in ``tree``.

        For ``from pkg import a, b``, yields both ``pkg`` (in case it's the
        module that defines ``a`` / ``b`` as names) and ``pkg.a`` / ``pkg.b``
        (in case they are submodules). The resolver tries each in turn.
        """
        try:
            cur_pkg_parts = list(cur_file.relative_to(repo_root).with_suffix("").parts[:-1])
        except ValueError:
            cur_pkg_parts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name
                continue
            if not isinstance(node, ast.ImportFrom):
                continue
            # Compute the absolute base package for this import statement.
            if node.level == 0:
                base = node.module or ""
            else:
                base_parts = cur_pkg_parts[: -node.level + 1] if node.level > 1 else list(cur_pkg_parts)
                if node.module:
                    base_parts.extend(node.module.split("."))
                base = ".".join(base_parts)
            if not base:
                continue
            yield base
            # Each imported name might itself be a submodule.
            for alias in node.names:
                if alias.name and alias.name != "*":
                    yield f"{base}.{alias.name}"

    @staticmethod
    def _resolve_module(name: str, repo_modules: dict[str, Path]) -> Path | None:
        """Resolve a dotted module name to a path in ``repo_modules`` if possible."""
        if not name:
            return None
        # Exact match, then progressively shorter prefixes (handles
        # `from services.helper import X` matching `services.helper`,
        # and `import services.helper` matching same).
        parts = name.split(".")
        while parts:
            candidate = ".".join(parts)
            if candidate in repo_modules:
                return repo_modules[candidate]
            parts.pop()
        return None

    @staticmethod
    def _parse_imports(tree: ast.AST, rel_path: str) -> list[Import]:
        out: list[Import] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.append(Import(module=alias.name, file=rel_path, line=node.lineno))
            elif isinstance(node, ast.ImportFrom):
                level = "." * (node.level or 0)
                mod = node.module or ""
                out.append(Import(module=f"{level}{mod}", file=rel_path, line=node.lineno))
        return out

    @staticmethod
    def _parse_service_calls(tree: ast.AST, rel_path: str) -> list[ServiceCall]:
        """Extract boto3-style calls with name-binding propagation.

        Handles:
            client = boto3.client('sqs'); client.send_message(...)
            ddb = boto3.resource('dynamodb'); ddb.Table('T').put_item(...)
            resource_name extracted from positional arg or a ``*Name`` kwarg.
        """
        # Pass 1: collect assignments and their bound services.
        service_of: dict[str, str] = {}
        assigns: list[tuple[str, ast.AST | None]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigns.append((node.targets[0].id, node.value))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                assigns.append((node.target.id, node.value))

        for name, value in assigns:
            svc = _service_from_factory(value)
            if svc is None and isinstance(value, ast.Call):
                svc = _service_from_chained_call(value, service_of)
                if svc is None and isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name):
                    svc = service_of.get(value.func.value.id)
            if svc:
                service_of[name] = svc

        # Pass 2: harvest call sites bound to a service.
        out: list[ServiceCall] = []
        from maf_generic_migrator_v1.platform_core.ir import Resource

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            svc: str | None = None
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                svc = service_of.get(node.func.value.id)
            if svc is None:
                svc = _service_from_chained_call(node, service_of)
            if svc is None:
                continue
            method = node.func.attr if isinstance(node.func, ast.Attribute) else "<call>"
            if method in _BOTO3_FACTORIES and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in {"boto3", "aioboto3"}:
                continue
            resource_name = _extract_resource_name(node)
            resource = None
            if resource_name:
                resource = Resource(
                    kind=_resource_kind_for_service(svc),
                    provider="aws",
                    name=resource_name,
                )
            out.append(
                ServiceCall(
                    service=svc,
                    operation=method,
                    resource=resource,
                    file=rel_path,
                    line=getattr(node, "lineno", 0),
                )
            )
        return out


# --------------------------------------------------------------------------- #
# Helpers (ported from legacy_mod_maf_v1's tree_sitter_py.py)
# --------------------------------------------------------------------------- #


def _service_from_factory(value: ast.AST | None) -> str | None:
    """Detect ``boto3.client('svc')`` / ``boto3.resource('svc')``."""
    if not isinstance(value, ast.Call):
        return None
    func = value.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _BOTO3_FACTORIES
        and isinstance(func.value, ast.Name)
        and func.value.id in {"boto3", "aioboto3"}
        and value.args
        and isinstance(value.args[0], ast.Constant)
        and isinstance(value.args[0].value, str)
    ):
        return value.args[0].value
    return None


def _service_from_chained_call(call: ast.Call, service_of: dict[str, str] | None = None) -> str | None:
    """Follow an attribute chain up to a boto3 factory or a known binding."""
    cur: ast.AST = call.func
    while isinstance(cur, ast.Attribute):
        cur = cur.value
        if isinstance(cur, ast.Call):
            svc = _service_from_factory(cur)
            if svc:
                return svc
        if service_of is not None and isinstance(cur, ast.Name):
            svc = service_of.get(cur.id)
            if svc:
                return svc
    return None


def _extract_resource_name(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for kw in call.keywords:
        if kw.arg in _NAME_KWARGS and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _resource_kind_for_service(service: str) -> str:
    return {
        "dynamodb": "table",
        "s3": "bucket",
        "sqs": "queue",
        "sns": "topic",
        "kinesis": "stream",
        "secretsmanager": "secret",
        "lambda": "function",
        "stepfunctions": "state_machine",
        "eventbridge": "rule",
    }.get(service, "resource")


_HANDLER_FILENAMES = ("function_app.py", "handler.py", "main.py", "app.py", "lambda_function.py")


def _guess_handler_entry(files: list[Path]) -> str | None:
    by_name = {f.name: f for f in files}
    for name in _HANDLER_FILENAMES:
        if name in by_name:
            return f"{by_name[name].name}:lambda_handler"
    return None


def _default_aws_resolver():
    """Lazy import of the AWS cartridge's SDK resolver.

    Kept lazy so the generic Python adapter remains useful for non-AWS
    cartridges. If the AWS cartridge isn't importable (Python2→3 style
    pair, etc.) the KG builder silently falls back to emitting
    ``external_call`` nodes without dataset edges — still useful for
    comprehension.
    """
    try:
        from maf_generic_migrator_v1.cartridges.aws_lambda_polyglot_to_azure_fn_py.aws_sdk_patterns import (
            resolve,
        )
    except Exception:  # noqa: BLE001
        return None
    return resolve
