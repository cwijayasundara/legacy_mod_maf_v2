"""Tests for the KG DOT / Mermaid renderers (AST call graph +
Fowler-style data-flow graph).
"""
from __future__ import annotations

from pathlib import Path

from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
    call_graph_dot,
    call_graph_mermaid,
    dataflow_dot,
    dataflow_mermaid,
    render_graphs,
)


def _span(line: int = 1) -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=line, end_line=line)


def _build_toy_store() -> NetworkXStore:
    """Tiny but realistic KG: one program with 2 paragraphs that PERFORM,
    reads one file + one SQL block, writes another dataset, bound to one
    JCL step. Exercises every edge kind the renderers care about.
    """
    s = NetworkXStore()
    # Program + structure
    s.add_node(KGNode(id="program:PAYROLL", kind="program", name="PAYROLL", span=_span()))
    s.add_node(KGNode(id="section:PAYROLL:MAIN", kind="section", name="MAIN", span=_span()))
    s.add_node(KGNode(id="paragraph:PAYROLL:READ-REC", kind="paragraph", name="READ-REC", span=_span()))
    s.add_node(KGNode(id="paragraph:PAYROLL:WRITE-REC", kind="paragraph", name="WRITE-REC", span=_span()))
    s.add_edge(KGEdge(source="program:PAYROLL", target="section:PAYROLL:MAIN", kind="contains"))
    s.add_edge(KGEdge(source="section:PAYROLL:MAIN", target="paragraph:PAYROLL:READ-REC", kind="contains"))
    s.add_edge(KGEdge(source="section:PAYROLL:MAIN", target="paragraph:PAYROLL:WRITE-REC", kind="contains"))
    s.add_edge(KGEdge(source="paragraph:PAYROLL:READ-REC", target="paragraph:PAYROLL:WRITE-REC", kind="performs"))

    # File + SQL block + external call
    s.add_node(KGNode(id="file:PAYROLL:PAYROLL-FILE", kind="file", name="PAYROLL-FILE", span=_span(),
                      attributes={"dd_name": "PAYIN"}))
    s.add_node(KGNode(id="sql_block:PAYROLL:INSERT:10", kind="sql_block", name="SQL-INSERT",
                      span=_span(10), attributes={"verb": "INSERT"}))
    s.add_node(KGNode(id="external_call:PAYROLL:NOTIFY:20", kind="external_call", name="NOTIFY",
                      span=_span(20)))
    s.add_edge(KGEdge(source="paragraph:PAYROLL:READ-REC", target="file:PAYROLL:PAYROLL-FILE", kind="reads"))
    s.add_edge(KGEdge(source="paragraph:PAYROLL:WRITE-REC", target="sql_block:PAYROLL:INSERT:10", kind="writes"))
    s.add_edge(KGEdge(source="paragraph:PAYROLL:WRITE-REC", target="external_call:PAYROLL:NOTIFY:20", kind="calls"))

    # JCL
    s.add_node(KGNode(id="job:PAYROLLJ", kind="job", name="PAYROLLJ", span=_span()))
    s.add_node(KGNode(id="step:PAYROLLJ:S1", kind="step", name="S1", span=_span(), attributes={"pgm": "PAYROLL"}))
    s.add_node(KGNode(id="dataset_ref:PROD.PAYROLL.MASTER", kind="dataset_ref",
                      name="PROD.PAYROLL.MASTER", span=_span()))
    s.add_edge(KGEdge(source="job:PAYROLLJ", target="step:PAYROLLJ:S1", kind="contains"))
    s.add_edge(KGEdge(source="program:PAYROLL", target="step:PAYROLLJ:S1", kind="step_of"))
    s.add_edge(KGEdge(source="step:PAYROLLJ:S1", target="dataset_ref:PROD.PAYROLL.MASTER",
                      kind="dd_of", attributes={"dd_name": "PAYIN"}))
    return s


# ------------------------------------------------------------------------- #
# Call graph (AST control-flow)
# ------------------------------------------------------------------------- #


def test_call_graph_dot_contains_every_control_flow_edge_kind():
    dot = call_graph_dot(_build_toy_store())
    # Header present
    assert dot.startswith("digraph call_graph")
    # Program name appears in a subgraph label
    assert "label=\"PAYROLL\"" in dot
    # Every control-flow edge kind shows up
    for kind in ("performs", "calls", "step_of"):
        assert f'label="{kind}"' in dot, f"missing edge kind: {kind}"


def test_call_graph_dot_does_not_include_data_only_edges():
    """The call graph is strictly control flow. ``reads`` / ``writes`` /
    ``dd_of`` / ``redefines`` must NOT appear — they belong in the
    dataflow diagram.
    """
    dot = call_graph_dot(_build_toy_store())
    for kind in ("reads", "writes", "dd_of", "redefines"):
        assert f'label="{kind}"' not in dot


def test_call_graph_mermaid_renders():
    mmd = call_graph_mermaid(_build_toy_store())
    assert mmd.startswith("```mermaid")
    assert "flowchart LR" in mmd
    assert "PAYROLL" in mmd
    # At least one performs edge
    assert "|performs|" in mmd


# ------------------------------------------------------------------------- #
# Dataflow graph (Fowler-style)
# ------------------------------------------------------------------------- #


def test_dataflow_dot_shows_program_to_resource_edges():
    dot = dataflow_dot(_build_toy_store())
    assert dot.startswith("digraph dataflow")
    # Reads + writes must surface
    assert 'label="reads"' in dot
    assert 'label="writes"' in dot
    # Program + dataset both present
    assert "PAYROLL" in dot
    assert "PROD.PAYROLL.MASTER" in dot


def test_dataflow_dot_aggregates_paragraph_edges_to_program_level():
    """Fowler's figures show data flow at the PROGRAM level, not per
    paragraph. The renderer must aggregate paragraph→file edges up to
    program→file.
    """
    dot = dataflow_dot(_build_toy_store())
    # The aggregated edge comes from the program, not a paragraph.
    # Specifically: the `program:PAYROLL` node must have a read edge
    # targeting the file, even though the source edge in the KG is
    # from a paragraph.
    assert "program_PAYROLL -> file_PAYROLL_PAYROLL_FILE" in dot
    # Paragraph-level data-flow edges must NOT appear — that's call-graph
    # territory, not dataflow territory.
    assert "paragraph_PAYROLL_READ_REC -> file_" not in dot


def test_dataflow_includes_jcl_dd_of_edges():
    dot = dataflow_dot(_build_toy_store())
    assert 'label="dd_of"' in dot


def test_dataflow_mermaid_renders():
    mmd = dataflow_mermaid(_build_toy_store())
    assert mmd.startswith("```mermaid")
    assert "|reads|" in mmd
    assert "|writes|" in mmd


# ------------------------------------------------------------------------- #
# render_graphs convenience writer
# ------------------------------------------------------------------------- #


def test_render_graphs_writes_four_files(tmp_path: Path):
    written = render_graphs(_build_toy_store(), tmp_path)
    assert (tmp_path / "call_graph.dot").is_file()
    assert (tmp_path / "call_graph.mmd").is_file()
    assert (tmp_path / "dataflow.dot").is_file()
    assert (tmp_path / "dataflow.mmd").is_file()
    # Four files at minimum. SVGs may or may not be generated depending
    # on whether ``dot`` is on PATH on this host — we don't require them.
    assert len(written) >= 4


def test_empty_store_renders_cleanly(tmp_path: Path):
    """A KG with no programs/nodes must still produce syntactically
    valid (if empty) diagrams — the pilot's smoke test depends on this.
    """
    s = NetworkXStore()
    call_dot = call_graph_dot(s)
    data_dot = dataflow_dot(s)
    assert call_dot.startswith("digraph call_graph")
    assert call_dot.rstrip().endswith("}")
    assert data_dot.startswith("digraph dataflow")
    assert data_dot.rstrip().endswith("}")
