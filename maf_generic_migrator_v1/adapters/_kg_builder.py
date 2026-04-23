"""Shared KG-population helper for polyglot Lambda adapters.

Each polyglot adapter (Python/Node/TypeScript/Java/C#) produces a flat
``UnitIR`` via ``extract_unit``. When the cartridge wants the full
``source -> AST -> KG -> LLM -> migrated-code`` pipeline, the same IR
plus a cartridge-supplied SDK resolver is enough to project the unit
into the platform ``KGStore`` as:

* one ``program`` node per unit (the Lambda module);
* ``paragraph`` nodes below the program, one per function (Python) or
  one per source file (non-Python) — the node kind the comprehension
  summarizer + CRUD matrix already expect;
* one ``external_call`` node per unique ``(service, operation)`` within
  the unit, linked to the paragraph that contains its call site;
* one ``dataset_ref`` node per named resource the unit touches, with
  ``reads`` / ``writes`` edges on the enclosing paragraph so the
  platform CRUD matrix sees the program's I/O without any cartridge-
  specific CRUD builder.

Emitting the edges from paragraphs (not from the program directly) is
the COBOL contract that ``build_crud_matrix`` + ``_collect_side_effects``
in ``platform_core.comprehension.business_spec`` walk. Matching that
contract lets the AWS-Lambda cartridge reuse both without forks.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from maf_generic_migrator_v1.platform_core.ir import ServiceCall, UnitIR
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    KGStore,
    SourceSpan,
)


#: Name of the synthetic paragraph emitted for non-Python units where we
#: don't have per-function AST granularity. One per source file so
#: CRUD / side-effect walks can still attribute calls by file.
_FILE_PARAGRAPH_PREFIX = "_file_"
#: Fallback paragraph name for call sites outside any tracked paragraph
#: (e.g. Python module-level boto3 client instantiation). Keeps the KG
#: self-consistent — every external_call hangs under some paragraph.
_MODULE_PARAGRAPH_NAME = "_module_"


def build_kg_from_ir(
    ir: UnitIR,
    store: KGStore,
    *,
    resolve_call: Callable[[ServiceCall], object | None] | None = None,
    runtime: str = "aws-lambda",
    function_paragraphs: "list[_ParagraphSpan] | None" = None,
) -> None:
    """Emit program + paragraph + external_call + dataset_ref nodes for ``ir``.

    ``function_paragraphs`` — when supplied (Python), describes
    per-function spans so external_calls inside each function land under
    that function's paragraph. When ``None``, we fall back to one
    synthetic paragraph per source file, with a module-level paragraph
    as a catch-all.

    ``resolve_call`` is the cartridge's SDK resolver (e.g.
    ``aws_sdk_patterns.resolve``). Unresolved calls still become
    ``external_call`` nodes (no dataset_ref edge) so comprehension can
    still summarize them.
    """
    program_id = ir.unit_id
    program_node_id = _program_node_id(program_id)

    program_span = _program_span(ir)
    store.add_node(
        KGNode(
            id=program_node_id,
            kind="program",
            name=program_id,
            span=program_span,
            attributes={
                "language": ir.language,
                "runtime": runtime,
                "root_path": ir.root_path,
                **({"handler_entry": ir.handler_entry} if ir.handler_entry else {}),
            },
        )
    )

    # Ensure each paragraph passed in is materialized up-front so we can
    # attribute calls by (file, line) without re-reading spans later.
    file_paragraphs: dict[str, str] = {}  # rel file -> paragraph_id (fallback bucket)
    spans: list[_ParagraphSpan] = list(function_paragraphs or ())

    for span in spans:
        _ensure_paragraph(store, program_node_id, program_id, span)

    def _paragraph_for(call: ServiceCall) -> str:
        # Prefer the function-granularity span when (file, line) lands inside.
        for span in spans:
            if span.file == call.file and span.start_line <= call.line <= span.end_line:
                return _paragraph_node_id(program_id, span.name)
        # Otherwise bucket by file — one synthetic paragraph per file.
        rel = call.file
        existing = file_paragraphs.get(rel)
        if existing is not None:
            return existing
        # If the caller already emitted function-level paragraphs but the
        # call site landed outside any of them (module-level boto3 client,
        # top-level script line), use a single module-level bucket per
        # program instead of one per file — avoids fragmenting side
        # effects across many near-empty paragraphs.
        if spans:
            span = _ParagraphSpan(
                name=_MODULE_PARAGRAPH_NAME,
                file=rel,
                start_line=1,
                end_line=1,
            )
        else:
            span = _ParagraphSpan(
                name=f"{_FILE_PARAGRAPH_PREFIX}{Path(rel).stem}",
                file=rel,
                start_line=1,
                end_line=1,
            )
        pid = _ensure_paragraph(store, program_node_id, program_id, span)
        file_paragraphs[rel] = pid
        return pid

    # De-dupe external_call emission within a single unit so repeated
    # ``s3.get_object`` call sites collapse into one node, while the
    # enclosing paragraph still gets a separate edge per call site for
    # evidence-tracking downstream.
    seen_calls: dict[tuple[str, str], str] = {}
    for call in ir.service_calls:
        ext_key = (call.service, call.operation)
        ext_id = seen_calls.get(ext_key)
        if ext_id is None:
            ext_id = f"external_call:{program_id}:{call.service}:{call.operation}"
            seen_calls[ext_key] = ext_id
            store.add_node(
                KGNode(
                    id=ext_id,
                    kind="external_call",
                    name=f"{call.service}.{call.operation}",
                    span=SourceSpan(
                        file=call.file,
                        start_line=max(1, call.line),
                        end_line=max(1, call.line),
                    ),
                    attributes={"service": call.service, "operation": call.operation},
                )
            )

        para_id = _paragraph_for(call)
        store.add_edge(
            KGEdge(
                source=para_id,
                target=ext_id,
                kind="calls",
                evidence=f"{call.file}:{call.line}",
            )
        )

        if resolve_call is None:
            continue
        ref = resolve_call(call)
        if ref is None:
            continue

        access = getattr(ref, "access", None)
        kind = getattr(ref, "kind", None)
        name = getattr(ref, "name", None)
        if not kind:
            continue

        ds_id = _dataset_node_id(kind, name)
        if not store.has_node(ds_id):
            store.add_node(
                KGNode(
                    id=ds_id,
                    kind="dataset_ref",
                    name=name or kind,
                    attributes={
                        "resource_kind": kind,
                        **({"resource_name": name} if name else {}),
                    },
                )
            )
        edge_kind = _access_to_edge_kind(access)
        # Emit the read/write edge from the *paragraph* (not the
        # external_call) so ``platform_core.pipeline.crud.build_crud_matrix``
        # — which walks paragraph → reads/writes — picks it up.
        store.add_edge(
            KGEdge(
                source=para_id,
                target=ds_id,
                kind=edge_kind,
                evidence=f"{call.file}:{call.line} {call.service}.{call.operation} ({access or 'uses'})",
                attributes={"access": access or "uses"},
            )
        )


class _ParagraphSpan:
    """Span + metadata for one paragraph node emitted under the program."""

    __slots__ = ("name", "file", "start_line", "end_line", "raw_text")

    def __init__(
        self,
        *,
        name: str,
        file: str,
        start_line: int,
        end_line: int,
        raw_text: str | None = None,
    ) -> None:
        self.name = name
        self.file = file
        self.start_line = start_line
        self.end_line = end_line
        self.raw_text = raw_text


def python_function_paragraphs(
    repo_root: Path, ir: UnitIR
) -> list[_ParagraphSpan]:
    """Enumerate ``(name, file, span)`` entries for every top-level function.

    Python-only helper: parses each ``.py`` file in the unit and emits one
    ``_ParagraphSpan`` per module-level function (sync or async). The
    Python adapter uses this as the ``function_paragraphs`` argument to
    ``build_kg_from_ir`` so external_call / dataset edges attach to the
    function that holds the call site.
    """
    import ast

    out: list[_ParagraphSpan] = []
    for rel in ir.files:
        path = repo_root / rel
        if path.suffix != ".py" or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text, filename=str(path))
        except (OSError, SyntaxError):
            continue
        lines = text.splitlines()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = node.lineno
            end = getattr(node, "end_lineno", start) or start
            raw = "\n".join(lines[start - 1 : end])
            out.append(
                _ParagraphSpan(
                    name=node.name,
                    file=rel,
                    start_line=start,
                    end_line=end,
                    raw_text=raw,
                )
            )
    return out


def _ensure_paragraph(
    store: KGStore,
    program_node_id: str,
    program_id: str,
    span: _ParagraphSpan,
) -> str:
    """Create the paragraph node + contains edge on first touch; return the id."""
    pid = _paragraph_node_id(program_id, span.name)
    if store.has_node(pid):
        return pid
    store.add_node(
        KGNode(
            id=pid,
            kind="paragraph",
            name=span.name,
            span=SourceSpan(
                file=span.file,
                start_line=max(1, span.start_line),
                end_line=max(span.start_line, span.end_line),
            ),
            raw_text=span.raw_text,
        )
    )
    store.add_edge(
        KGEdge(
            source=program_node_id,
            target=pid,
            kind="contains",
            evidence=f"{span.file}:{span.start_line}",
        )
    )
    return pid


def _program_node_id(program_id: str) -> str:
    return f"program:{program_id}"


def _paragraph_node_id(program_id: str, name: str) -> str:
    return f"paragraph:{program_id}:{name}"


def _dataset_node_id(kind: str, name: str | None) -> str:
    return f"dataset_ref:{kind}:{name or '_unnamed'}"


def _program_span(ir: UnitIR) -> SourceSpan | None:
    if not ir.files:
        return None
    end = max(1, ir.loc or 1)
    return SourceSpan(file=ir.files[0], start_line=1, end_line=end)


def _access_to_edge_kind(access: str | None) -> str:
    """Map cartridge resolver access kinds onto platform edge kinds.

    The platform KG schema uses ``reads`` / ``writes`` / ``calls`` (no
    native ``produces`` / ``consumes``). Keep the distinction as an edge
    attribute while bucketing into the closest match: ``produces`` ->
    ``writes`` (unit puts data on the queue/topic), ``consumes`` ->
    ``reads`` (unit pulls data off it). ``invokes`` stays as ``calls``
    since it's semantically "triggers another unit".
    """
    return {
        "reads": "reads",
        "writes": "writes",
        "produces": "writes",
        "consumes": "reads",
        "invokes": "calls",
    }.get(access or "", "calls")
