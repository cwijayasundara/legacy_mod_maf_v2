"""Tests for the KG schema + NetworkX store + traversal helpers."""
from __future__ import annotations

import pytest

from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
    ancestors,
    descendants,
    post_order,
    roots,
)


def _node(node_id: str, kind: str, name: str | None = None, **extra) -> KGNode:
    return KGNode(
        id=node_id,
        kind=kind,
        name=name or node_id,
        span=SourceSpan(file="PAYROLL.cbl", start_line=1, end_line=10),
        raw_text=extra.pop("raw_text", None),
        attributes=extra,
    )


@pytest.fixture()
def store() -> NetworkXStore:
    """Three-level toy KG:

        PAYROLL (program)
          ├─ COMPUTE-GROSS (section)
          │    └─ ADD-OT (paragraph)
          └─ WRITE-OUTPUT (section)
               └─ EMIT-ROW (paragraph)
    """
    s = NetworkXStore()
    s.add_node(_node("PAYROLL", "program"))
    s.add_node(_node("COMPUTE-GROSS", "section"))
    s.add_node(_node("ADD-OT", "paragraph"))
    s.add_node(_node("WRITE-OUTPUT", "section"))
    s.add_node(_node("EMIT-ROW", "paragraph"))
    for src, dst in [
        ("PAYROLL", "COMPUTE-GROSS"),
        ("PAYROLL", "WRITE-OUTPUT"),
        ("COMPUTE-GROSS", "ADD-OT"),
        ("WRITE-OUTPUT", "EMIT-ROW"),
    ]:
        s.add_edge(KGEdge(source=src, target=dst, kind="contains"))
    return s


def test_add_and_get_node():
    s = NetworkXStore()
    s.add_node(_node("A", "program"))
    got = s.get_node("A")
    assert got is not None
    assert got.kind == "program"
    assert got.name == "A"
    assert s.has_node("A") is True
    assert s.get_node("missing") is None


def test_add_edge_rejects_missing_endpoints():
    s = NetworkXStore()
    s.add_node(_node("A", "program"))
    with pytest.raises(KeyError):
        s.add_edge(KGEdge(source="A", target="GHOST", kind="contains"))


def test_add_edge_dedupes_identical_edges():
    s = NetworkXStore()
    s.add_node(_node("A", "program"))
    s.add_node(_node("B", "section"))
    s.add_edge(KGEdge(source="A", target="B", kind="contains", evidence="line 1"))
    s.add_edge(KGEdge(source="A", target="B", kind="contains", evidence="line 1"))
    assert len(list(s.iter_edges())) == 1
    # Different evidence -> not a duplicate.
    s.add_edge(KGEdge(source="A", target="B", kind="contains", evidence="line 9"))
    assert len(list(s.iter_edges())) == 2


def test_iter_nodes_filters_by_kind(store: NetworkXStore):
    programs = list(store.iter_nodes(kind="program"))
    assert [n.id for n in programs] == ["PAYROLL"]
    paragraphs = sorted(n.id for n in store.iter_nodes(kind="paragraph"))
    assert paragraphs == ["ADD-OT", "EMIT-ROW"]


def test_neighbors_direction_and_filter(store: NetworkXStore):
    out = store.neighbors("PAYROLL", direction="out", edge_kinds=["contains"])
    assert sorted(n.id for n in out) == ["COMPUTE-GROSS", "WRITE-OUTPUT"]

    inbound = store.neighbors("COMPUTE-GROSS", direction="in", edge_kinds=["contains"])
    assert [n.id for n in inbound] == ["PAYROLL"]

    both = store.neighbors("COMPUTE-GROSS", direction="both", edge_kinds=["contains"])
    assert sorted(n.id for n in both) == ["ADD-OT", "PAYROLL"]


def test_neighbors_invalid_direction_raises(store: NetworkXStore):
    with pytest.raises(ValueError):
        store.neighbors("PAYROLL", direction="sideways")


def test_update_node_merges_fields(store: NetworkXStore):
    store.update_node("ADD-OT", llm_summary="adds overtime")
    assert store.get_node("ADD-OT").llm_summary == "adds overtime"
    # Other fields preserved.
    assert store.get_node("ADD-OT").kind == "paragraph"


def test_update_node_missing_raises():
    s = NetworkXStore()
    with pytest.raises(KeyError):
        s.update_node("nope", llm_summary="x")


def test_roots(store: NetworkXStore):
    assert [n.id for n in roots(store)] == ["PAYROLL"]


def test_post_order_bottom_up(store: NetworkXStore):
    order = [n.id for n in post_order(store, "PAYROLL")]
    # Leaves must come before their parents.
    assert order.index("ADD-OT") < order.index("COMPUTE-GROSS")
    assert order.index("EMIT-ROW") < order.index("WRITE-OUTPUT")
    assert order.index("COMPUTE-GROSS") < order.index("PAYROLL")
    assert order.index("WRITE-OUTPUT") < order.index("PAYROLL")
    assert order[-1] == "PAYROLL"
    assert len(order) == 5


def test_descendants_and_ancestors(store: NetworkXStore):
    descs = sorted(n.id for n in descendants(store, "PAYROLL"))
    assert descs == ["ADD-OT", "COMPUTE-GROSS", "EMIT-ROW", "WRITE-OUTPUT"]
    ancs = sorted(n.id for n in ancestors(store, "ADD-OT"))
    assert ancs == ["COMPUTE-GROSS", "PAYROLL"]


def test_snapshot_and_load_roundtrip(store: NetworkXStore):
    snap = store.snapshot()
    assert len(snap.nodes) == 5
    assert len(snap.edges) == 4

    fresh = NetworkXStore()
    fresh.load(snap)
    assert sorted(n.id for n in fresh.iter_nodes()) == sorted(
        n.id for n in store.iter_nodes()
    )
    assert len(list(fresh.iter_edges())) == 4


def test_post_order_handles_cycles():
    """Summarizer mustn't loop forever on (rare) cyclic graphs."""
    s = NetworkXStore()
    s.add_node(_node("A", "program"))
    s.add_node(_node("B", "section"))
    s.add_edge(KGEdge(source="A", target="B", kind="contains"))
    s.add_edge(KGEdge(source="B", target="A", kind="contains"))
    order = [n.id for n in post_order(s, "A")]
    assert set(order) == {"A", "B"}
