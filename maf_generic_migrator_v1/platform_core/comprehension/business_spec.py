"""Business spec renderer.

A business spec is the analyst-consumable Markdown document produced for
each program after the comprehension pipeline runs. It is also the
translator's primary input in Phase 4 — the translator sees this spec plus
pointers to per-paragraph summaries, never the raw COBOL.

What goes in a spec:

* **Prose parts** (Purpose, Side-effect descriptions, Key invariants)
  come from the ``program``-kind LLM summary, parsed out of the structured
  key/value block the program.md prompt produces.
* **Deterministic parts** (Inputs, Outputs, explicit side-effect call
  sites) come from the ``CRUDMatrix`` + KG edges. LLM output is never
  trusted to enumerate datasets — the matrix is ground truth.

This split is load-bearing: it's the reason the spec can be trusted as
translator input. Prose describes *intent*; the deterministic sections
describe *contract*.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from maf_generic_migrator_v1.platform_core.kg import KGNode, KGStore
from maf_generic_migrator_v1.platform_core.pipeline.crud import (
    CRUDMatrix,
    ResourceRef,
    build_crud_matrix,
)

# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class BusinessSpec:
    """One program's business spec.

    The Markdown is the primary artifact; the structured fields are exposed
    so downstream code (translators, rubrics, dashboards) can query without
    re-parsing the Markdown.
    """

    program_id: str
    purpose: str
    inputs: list[ResourceRef]
    outputs: list[ResourceRef]
    side_effects: list[str]
    key_invariants: list[str]
    paragraph_summaries: list[tuple[str, str]]       # (paragraph_name, summary)
    section_summaries: list[tuple[str, str]]         # (section_name, summary)
    markdown: str

    # Convenience — same info rolled up for the translator prompt.
    def dataset_inputs(self) -> list[str]:
        return [r.name for r in self.inputs if r.kind == "dataset"]

    def dataset_outputs(self) -> list[str]:
        return [r.name for r in self.outputs if r.kind == "dataset"]


# --------------------------------------------------------------------------- #
# Parsing the structured LLM block
# --------------------------------------------------------------------------- #


_FIELDS = ("Purpose", "Inputs", "Outputs", "Side effects", "Key invariants")


def _parse_structured(summary: str | None) -> dict[str, str]:
    """Parse the ``key: value`` block produced by ``program.md``.

    Tolerant: missing keys return empty strings, extra prose around the
    block is ignored. Values may span multiple lines up to the next
    recognized key (needed for "Key invariants" which is typically a
    bulleted list).
    """
    if not summary:
        return {k: "" for k in _FIELDS}

    text = summary.strip()
    # Accept content inside ``` fences in case the LLM wraps the block.
    if "```" in text:
        parts = text.split("```")
        text = max(parts, key=len)

    pattern = re.compile(
        r"^(?P<key>"
        + "|".join(re.escape(f) for f in _FIELDS)
        + r")\s*:\s*(?P<value>.*?)(?=^(?:"
        + "|".join(re.escape(f) for f in _FIELDS)
        + r")\s*:|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    found: dict[str, str] = {k: "" for k in _FIELDS}
    for match in pattern.finditer(text):
        key = match.group("key")
        value = match.group("value").strip()
        found[key] = value
    return found


def _parse_invariants(value: str) -> list[str]:
    """Extract one-per-line invariants from a bulleted or newline-separated value."""
    if not value or value.lower().startswith("(none"):
        return []
    items: list[str] = []
    for line in value.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"^[-*]\s+", "", cleaned)
        if cleaned:
            items.append(cleaned)
    return items


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def render_business_spec(
    store: KGStore,
    program_id: str,
    *,
    matrix: CRUDMatrix | None = None,
    target_style: str | None = None,
) -> BusinessSpec:
    """Build a ``BusinessSpec`` for one program.

    Requires that the comprehension summarizer has already populated
    ``llm_summary`` on the program node (and ideally its children) — the
    caller is expected to have run the summarizer first.

    ``target_style`` (when provided) tightens the Inputs/Outputs filter:

    * ``batch`` — include both paragraph-R/W datasets and JCL
      touches-only datasets. Touches-only is genuine batch signal.
    * ``subroutine`` / ``cics`` — exclude touches-only. Library code's
      inputs come from LINKAGE SECTION; CICS transactions from maps and
      SQL. JCL wrappers that happen to also batch-pipe them are noise
      at the spec level.
    * ``None`` — legacy behaviour; include touches-only as inputs.
    """
    matrix = matrix or build_crud_matrix(store)
    program = _find_program(store, program_id)
    if program is None:
        raise KeyError(f"no program node with name={program_id!r}")

    structured = _parse_structured(program.llm_summary)
    purpose = structured["Purpose"] or "(purpose not yet summarized)"
    side_effects_prose = structured["Side effects"]
    invariants = _parse_invariants(structured["Key invariants"])

    include_touches = target_style in (None, "batch")
    inputs = _dataset_inputs(matrix, program_id, include_touches_only=include_touches)
    outputs = _dataset_outputs(matrix, program_id)
    side_effects = _collect_side_effects(store, program.id, side_effects_prose)

    section_summaries = _collect_summaries(store, program.id, kind="section")
    paragraph_summaries = _collect_summaries(store, program.id, kind="paragraph")

    markdown = _render_markdown(
        program_id=program_id,
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        side_effects=side_effects,
        invariants=invariants,
        section_summaries=section_summaries,
        paragraph_summaries=paragraph_summaries,
    )

    return BusinessSpec(
        program_id=program_id,
        purpose=purpose,
        inputs=inputs,
        outputs=outputs,
        side_effects=side_effects,
        key_invariants=invariants,
        paragraph_summaries=paragraph_summaries,
        section_summaries=section_summaries,
        markdown=markdown,
    )


def render_all_business_specs(
    store: KGStore,
    *,
    matrix: CRUDMatrix | None = None,
    classify: "Callable[[str], str | None] | None" = None,
) -> list[BusinessSpec]:
    """Render one spec per program in the KG, alphabetical by program_id.

    Passing ``classify`` — a callable returning the target style for a
    program — lets the renderer tighten the Inputs filter per program
    (subroutines and CICS transactions don't want JCL touches-only
    datasets). With no classifier, every program is treated as
    target-style-unknown and the legacy "include everything" filter
    applies.
    """
    matrix = matrix or build_crud_matrix(store)
    out: list[BusinessSpec] = []
    for program in sorted(store.iter_nodes(kind="program"), key=lambda n: n.name):
        style = classify(program.name) if classify else None
        out.append(
            render_business_spec(store, program.name, matrix=matrix, target_style=style)
        )
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _find_program(store: KGStore, program_id: str) -> KGNode | None:
    for node in store.iter_nodes(kind="program"):
        if node.name == program_id:
            return node
    return None


# JCL "infrastructure" DDs that point at loadlibs, log spools, and system
# procs — never business inputs/outputs. Filtered out of the spec so the
# translator doesn't try to migrate them. The comparison is against any
# occurrence of "DD <NAME>" in the CRUD evidence strings.
_INFRA_DD_NAMES = {
    "STEPLIB", "JOBLIB", "SYSLIB", "SYSPROC",
    "SYSIN", "SYSOUT", "SYSUDUMP", "SYSABEND", "SYSPRINT",
}


def _is_infra_touches_only(ops) -> bool:
    """True if this touches_only row is runtime plumbing (STEPLIB, SYSOUT, …)."""
    if not ops.touches_only:
        return False
    # Evidence strings end with ``DD <name>`` when we built them from JCL.
    for ev in ops.evidence:
        for dd in _INFRA_DD_NAMES:
            if ev.endswith(f"DD {dd}") or f" DD {dd}" in ev:
                return True
    return False


def _dataset_inputs(
    matrix: CRUDMatrix,
    program_id: str,
    *,
    include_touches_only: bool = True,
) -> list[ResourceRef]:
    """Datasets the program reads (or touches as JCL input only).

    Excludes pure-writer datasets (those belong to Outputs) and
    infrastructure DDs (STEPLIB / SYSOUT / …). Excludes touches-only
    datasets entirely when ``include_touches_only`` is False —
    subroutines and CICS transactions don't consume data through
    dataset bindings, so ad-hoc JCL wrappers are noise for them.
    """
    out: list[ResourceRef] = []
    for r in matrix.resources_touched_by(program_id):
        if r.kind != "dataset":
            continue
        ops = matrix.ops(program_id, r)
        if ops is None:
            continue
        if _is_infra_touches_only(ops):
            continue
        if _is_pure_writer(matrix, program_id, r):
            continue
        if ops.read:
            out.append(r)
        elif include_touches_only and ops.touches_only:
            out.append(r)
    return out


def _dataset_outputs(matrix: CRUDMatrix, program_id: str) -> list[ResourceRef]:
    out: list[ResourceRef] = []
    for r in matrix.resources_touched_by(program_id):
        if r.kind != "dataset":
            continue
        ops = matrix.ops(program_id, r)
        if ops is None:
            continue
        if _is_infra_touches_only(ops):
            continue
        if ops.create or ops.update or ops.delete:
            out.append(r)
    return out


def _is_pure_writer(matrix: CRUDMatrix, program_id: str, resource: ResourceRef) -> bool:
    ops = matrix.ops(program_id, resource)
    if ops is None:
        return False
    mutates = ops.create or ops.update or ops.delete
    return mutates and not ops.read


def _collect_side_effects(store: KGStore, program_node_id: str, prose: str) -> list[str]:
    """Explicit side-effect call sites pulled from the KG.

    Never trust LLM prose for enumeration — walk outgoing ``calls``,
    ``reads``, and ``writes`` edges from every paragraph descendant and
    report any ``external_call`` / ``txn_call`` / ``sql_block`` targets.
    Prose from the LLM is preserved as a separate "narrative" bullet at
    the end, clearly labeled.

    Side-effect nodes are linked by ``calls``/``writes``/``reads`` edges —
    NOT by ``contains`` — so a naive contains-only walk misses them.
    """
    effects: list[str] = []
    seen: set[str] = set()

    def _record(node: KGNode) -> None:
        if node.kind == "external_call":
            entry = f"CALL {node.name}"
        elif node.kind == "txn_call":
            verb = node.attributes.get("verb", "?")
            entry = f"EXEC CICS {verb}"
        elif node.kind == "sql_block":
            verb = node.attributes.get("verb", "?")
            entry = f"EXEC SQL {verb}"
        else:
            return
        if entry in seen:
            return
        seen.add(entry)
        effects.append(entry)

    for node in _descendants(store, program_node_id):
        for neighbor in store.neighbors(
            node.id, direction="out", edge_kinds=["calls", "writes", "reads"]
        ):
            _record(neighbor)

    prose = (prose or "").strip()
    if prose and prose.lower() not in {"none", "(none)"}:
        effects.append(f"(LLM narrative) {prose}")
    return effects


def _descendants(store: KGStore, root_id: str) -> Iterable[KGNode]:
    """All nodes reachable from root via ``contains`` edges."""
    seen: set[str] = set()
    queue = [root_id]
    while queue:
        nid = queue.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for n in store.neighbors(nid, direction="out", edge_kinds=["contains"]):
            if n.id not in seen:
                queue.append(n.id)
                yield n


def _collect_summaries(
    store: KGStore, program_node_id: str, *, kind: str
) -> list[tuple[str, str]]:
    out: list[tuple[str, int, str]] = []
    for node in _descendants(store, program_node_id):
        if node.kind != kind or not node.llm_summary:
            continue
        line = node.span.start_line if node.span else 0
        out.append((node.name, line, node.llm_summary))
    out.sort(key=lambda t: t[1])
    return [(name, summary) for name, _, summary in out]


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #


def _render_markdown(
    *,
    program_id: str,
    purpose: str,
    inputs: list[ResourceRef],
    outputs: list[ResourceRef],
    side_effects: list[str],
    invariants: list[str],
    section_summaries: list[tuple[str, str]],
    paragraph_summaries: list[tuple[str, str]],
) -> str:
    def _listify(items: Iterable[str]) -> str:
        rows = list(items)
        if not rows:
            return "- (none)"
        return "\n".join(f"- {row}" for row in rows)

    parts = [
        f"# Program Spec: {program_id}",
        "",
        "## Purpose",
        purpose,
        "",
        "## Inputs",
        _listify(r.display for r in inputs),
        "",
        "## Outputs",
        _listify(r.display for r in outputs),
        "",
        "## Side effects",
        _listify(side_effects),
        "",
        "## Key invariants",
        _listify(invariants) if invariants else "- (none identified)",
        "",
    ]

    if section_summaries:
        parts.append("## Sections")
        for name, summary in section_summaries:
            parts.append(f"### {name}")
            parts.append(summary)
            parts.append("")

    if paragraph_summaries:
        parts.append("## Paragraphs")
        for name, summary in paragraph_summaries:
            parts.append(f"### {name}")
            parts.append(summary)
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


__all__ = [
    "BusinessSpec",
    "render_business_spec",
    "render_all_business_specs",
]
