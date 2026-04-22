"""Tests for seam-aware wave scheduling in the planner."""
from __future__ import annotations

from pathlib import Path

from maf_generic_migrator_v1.platform_core.cartridge import (
    AgentSpec,
    EcosystemSignature,
    MigrationCartridge,
)
from maf_generic_migrator_v1.platform_core.ir import (
    Contract,
    CrossEdge,
    DependencyGraph,
    Inventory,
    UnitIR,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)
from maf_generic_migrator_v1.platform_core.pipeline.planner import plan_migration


class _DummyCartridge(MigrationCartridge):
    id = "dummy"
    source = EcosystemSignature(language="cobol")
    target = EcosystemSignature(language="java")

    def adapters(self):
        return {}

    def unit_classifier(self, repo_root):
        return []

    def translator_agents(self):
        return [AgentSpec(name="t", role="translator", system_prompt_path="x")]


def _make_inventory(*unit_ids: str) -> Inventory:
    units = [
        UnitIR(
            unit_id=uid,
            kind="module",
            language="cobol",
            root_path=uid,
            contract=Contract(),
        )
        for uid in unit_ids
    ]
    return Inventory(repo_root="/x", cartridge_id="dummy", units=units)


def _flat_graph(*unit_ids: str) -> DependencyGraph:
    """All units in the same wave (no cross-unit edges)."""
    return DependencyGraph(units=list(unit_ids), edges=[])


def _span() -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=1, end_line=1)


def test_without_kg_falls_back_to_alphabetical():
    """Pre-existing behaviour must not change for cartridges that don't
    populate a KG.
    """
    inventory = _make_inventory("CHARLIE", "ALPHA", "BRAVO")
    graph = _flat_graph("CHARLIE", "ALPHA", "BRAVO")
    backlog = plan_migration(inventory, graph, _DummyCartridge())
    assert len(backlog.waves) == 1
    assert [i.unit_id for i in backlog.waves[0]] == ["ALPHA", "BRAVO", "CHARLIE"]


def test_kg_store_reorders_wave_by_seam_score():
    """Build a KG where PAYROLL writes to a high-scoring shared
    dataset while SOLO only touches an isolated file. PAYROLL should
    sort first in the wave even though alphabetically SOLO precedes
    PAYROLL's friends.
    """
    store = NetworkXStore()

    # Three programs.
    for pid in ("PAYROLL", "REPORTER", "SOLO"):
        store.add_node(KGNode(id=f"program:{pid}", kind="program", name=pid, span=_span()))
        store.add_node(KGNode(id=f"paragraph:{pid}:MAIN", kind="paragraph", name="MAIN", span=_span()))
        store.add_edge(KGEdge(source=f"program:{pid}", target=f"paragraph:{pid}:MAIN", kind="contains"))

    # Shared high-fanout dataset — PAYROLL writes, REPORTER reads.
    store.add_node(KGNode(id="dataset_ref:SHARED", kind="dataset_ref", name="SHARED", span=_span()))
    store.add_edge(KGEdge(source="paragraph:PAYROLL:MAIN", target="dataset_ref:SHARED", kind="writes"))
    store.add_edge(KGEdge(source="paragraph:REPORTER:MAIN", target="dataset_ref:SHARED", kind="reads"))

    # SOLO touches an isolated low-observability file (not a dataset).
    store.add_node(KGNode(id="file:SOLO:PRIVATE-FD", kind="file", name="PRIVATE-FD", span=_span()))
    store.add_edge(KGEdge(source="paragraph:SOLO:MAIN", target="file:SOLO:PRIVATE-FD", kind="reads"))

    inventory = _make_inventory("PAYROLL", "REPORTER", "SOLO")
    graph = _flat_graph("PAYROLL", "REPORTER", "SOLO")
    backlog = plan_migration(inventory, graph, _DummyCartridge(), kg_store=store)

    order = [item.unit_id for item in backlog.waves[0]]
    # PAYROLL + REPORTER both sit next to the top-score seam (SHARED
    # dataset, seam score 1.0 in the default weights); SOLO sits next to
    # a file-kind seam (score ~0.48). PAYROLL and REPORTER sort ahead
    # of SOLO.
    assert order.index("SOLO") > order.index("PAYROLL")
    assert order.index("SOLO") > order.index("REPORTER")
    # Within the same seam-score, alphabetical tiebreak applies.
    assert order.index("PAYROLL") < order.index("REPORTER")


def test_deterministic_ordering_with_equal_scores():
    """Programs tied on seam score must still sort deterministically
    (alphabetically by unit_id).
    """
    store = NetworkXStore()
    for pid in ("BRAVO", "ALPHA"):
        store.add_node(KGNode(id=f"program:{pid}", kind="program", name=pid, span=_span()))

    inventory = _make_inventory("BRAVO", "ALPHA")
    graph = _flat_graph("BRAVO", "ALPHA")
    backlog = plan_migration(inventory, graph, _DummyCartridge(), kg_store=store)
    order = [i.unit_id for i in backlog.waves[0]]
    assert order == ["ALPHA", "BRAVO"]
