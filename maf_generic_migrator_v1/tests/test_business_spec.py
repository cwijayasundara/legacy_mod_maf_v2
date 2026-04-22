"""Tests for the business-spec renderer.

Business specs are the single most important artifact downstream. Two
invariants must hold:

* Inputs / Outputs are grounded in the CRUD matrix — never from LLM prose.
* Side-effect call sites (CALL, EXEC SQL, EXEC CICS) are enumerated from
  KG edges, not from LLM prose.
"""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.comprehension.business_spec import (
    render_business_spec,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)


def _span(line: int = 1) -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=line, end_line=line)


def _baseline_store() -> NetworkXStore:
    """PAYROLL-ish store: one program, one paragraph, one reader FD, one
    writer FD, one SQL block, one external CALL, and JCL wiring to two
    datasets plus STEPLIB + SYSOUT.
    """
    s = NetworkXStore()
    s.add_node(KGNode(id="program:P", kind="program", name="P", span=_span()))
    s.add_node(KGNode(id="paragraph:P:MAIN", kind="paragraph", name="MAIN", span=_span(2)))
    s.add_edge(KGEdge(source="program:P", target="paragraph:P:MAIN", kind="contains"))

    # FDs with DD bindings
    s.add_node(
        KGNode(
            id="file:P:IN-FD",
            kind="file",
            name="IN-FD",
            span=_span(),
            attributes={"dd_name": "INDD"},
        )
    )
    s.add_node(
        KGNode(
            id="file:P:OUT-FD",
            kind="file",
            name="OUT-FD",
            span=_span(),
            attributes={"dd_name": "OUTDD"},
        )
    )
    s.add_edge(KGEdge(source="program:P", target="file:P:IN-FD", kind="contains"))
    s.add_edge(KGEdge(source="program:P", target="file:P:OUT-FD", kind="contains"))
    s.add_edge(KGEdge(source="paragraph:P:MAIN", target="file:P:IN-FD", kind="reads"))
    s.add_edge(KGEdge(source="paragraph:P:MAIN", target="file:P:OUT-FD", kind="writes"))

    # Side-effect nodes
    s.add_node(
        KGNode(
            id="external_call:P:NOTIFY:5",
            kind="external_call",
            name="NOTIFY",
            span=_span(5),
        )
    )
    s.add_edge(KGEdge(source="paragraph:P:MAIN", target="external_call:P:NOTIFY:5", kind="calls"))
    s.add_node(
        KGNode(
            id="sql_block:P:0:6",
            kind="sql_block",
            name="SQL-INSERT",
            span=_span(6),
            attributes={"verb": "INSERT"},
        )
    )
    s.add_edge(KGEdge(source="paragraph:P:MAIN", target="sql_block:P:0:6", kind="writes"))

    # JCL
    s.add_node(KGNode(id="job:J", kind="job", name="J", span=_span()))
    s.add_node(KGNode(id="step:J:S1", kind="step", name="S1", span=_span(), attributes={"pgm": "P"}))
    s.add_node(KGNode(id="dataset_ref:PROD.IN", kind="dataset_ref", name="PROD.IN", span=_span()))
    s.add_node(KGNode(id="dataset_ref:PROD.OUT", kind="dataset_ref", name="PROD.OUT", span=_span()))
    s.add_node(KGNode(id="dataset_ref:PROD.LOADLIB", kind="dataset_ref", name="PROD.LOADLIB", span=_span()))
    s.add_node(KGNode(id="dataset_ref:SYSOUT-*", kind="dataset_ref", name="SYSOUT-*", span=_span()))
    s.add_edge(KGEdge(source="job:J", target="step:J:S1", kind="contains"))
    s.add_edge(KGEdge(source="program:P", target="step:J:S1", kind="step_of"))
    s.add_edge(
        KGEdge(source="step:J:S1", target="dataset_ref:PROD.IN", kind="dd_of",
               attributes={"dd_name": "INDD"}, evidence="J.jcl:3 DD INDD")
    )
    s.add_edge(
        KGEdge(source="step:J:S1", target="dataset_ref:PROD.OUT", kind="dd_of",
               attributes={"dd_name": "OUTDD"}, evidence="J.jcl:4 DD OUTDD")
    )
    s.add_edge(
        KGEdge(source="step:J:S1", target="dataset_ref:PROD.LOADLIB", kind="dd_of",
               attributes={"dd_name": "STEPLIB"}, evidence="J.jcl:5 DD STEPLIB")
    )
    s.add_edge(
        KGEdge(source="step:J:S1", target="dataset_ref:SYSOUT-*", kind="dd_of",
               attributes={"dd_name": "SYSOUT"}, evidence="J.jcl:6 DD SYSOUT")
    )
    return s


def _set_program_summary(store: NetworkXStore, summary: str) -> None:
    store.update_node("program:P", llm_summary=summary)
    store.update_node("paragraph:P:MAIN", llm_summary="does the work")


STRUCTURED = (
    "Purpose: loads customers then notifies auditors.\n"
    "Inputs: PROD.IN\n"
    "Outputs: PROD.OUT\n"
    "Side effects: CALL NOTIFY, EXEC SQL INSERT\n"
    "Key invariants:\n"
    "  - input sorted by CUSTOMER-ID\n"
    "  - COMP-3 totals never overflow 9(7)V99\n"
)


def test_purpose_comes_from_llm_summary():
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    spec = render_business_spec(s, "P")
    assert "loads customers then notifies auditors" in spec.purpose


def test_inputs_come_from_crud_matrix_not_llm():
    """Even if the LLM says ``Inputs: SOMETHING-WRONG``, the spec must
    show the CRUD-derived real dataset.
    """
    s = _baseline_store()
    bad_llm = STRUCTURED.replace("PROD.IN", "LLM-HALLUCINATION")
    _set_program_summary(s, bad_llm)
    spec = render_business_spec(s, "P")

    assert spec.dataset_inputs() == ["PROD.IN"]
    assert "LLM-HALLUCINATION" not in spec.markdown


def test_outputs_come_from_crud_matrix():
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    spec = render_business_spec(s, "P")
    assert spec.dataset_outputs() == ["PROD.OUT"]


def test_infra_dds_filtered_from_inputs():
    """STEPLIB and SYSOUT must never appear in Inputs."""
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    spec = render_business_spec(s, "P")
    input_names = [r.name for r in spec.inputs]
    assert "PROD.LOADLIB" not in input_names
    assert "SYSOUT-*" not in input_names


def test_side_effects_include_deterministic_calls():
    """CALL NOTIFY and EXEC SQL INSERT must appear even if the LLM omits
    them, because they're enumerated from the KG.
    """
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED.replace("Side effects: CALL NOTIFY, EXEC SQL INSERT", "Side effects: none"))
    spec = render_business_spec(s, "P")
    assert "CALL NOTIFY" in spec.side_effects
    assert "EXEC SQL INSERT" in spec.side_effects


def test_side_effects_include_llm_narrative_when_present():
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    spec = render_business_spec(s, "P")
    assert any("(LLM narrative)" in entry for entry in spec.side_effects)


def test_key_invariants_parsed_from_bulleted_list():
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    spec = render_business_spec(s, "P")
    assert len(spec.key_invariants) == 2
    assert "COMP-3" in spec.key_invariants[1]


def test_missing_program_raises():
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    import pytest
    with pytest.raises(KeyError):
        render_business_spec(s, "NO-SUCH-PROGRAM")


def test_markdown_has_expected_sections():
    s = _baseline_store()
    _set_program_summary(s, STRUCTURED)
    spec = render_business_spec(s, "P")
    md = spec.markdown
    for heading in (
        "# Program Spec: P",
        "## Purpose",
        "## Inputs",
        "## Outputs",
        "## Side effects",
        "## Key invariants",
    ):
        assert heading in md


def test_graceful_when_llm_summary_unset():
    """Before the summarizer runs, the program has no llm_summary. The
    renderer must still produce a spec with placeholder purpose and
    deterministic I/O sections.
    """
    s = _baseline_store()
    # No _set_program_summary call.
    spec = render_business_spec(s, "P")
    assert spec.purpose.startswith("(purpose not yet summarized)")
    assert spec.dataset_inputs() == ["PROD.IN"]        # still from CRUD
    assert spec.dataset_outputs() == ["PROD.OUT"]
    assert "CALL NOTIFY" in spec.side_effects          # still from KG
