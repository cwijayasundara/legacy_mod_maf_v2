"""Tests for the COBOL → Java target-style classifier."""
from __future__ import annotations

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.target_style import (
    classify_program,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)


def _span() -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=1, end_line=1)


def _program(store: NetworkXStore, pid: str) -> None:
    store.add_node(KGNode(id=f"program:{pid}", kind="program", name=pid, span=_span()))
    store.add_node(KGNode(id=f"paragraph:{pid}:MAIN", kind="paragraph", name="MAIN", span=_span()))
    store.add_edge(KGEdge(source=f"program:{pid}", target=f"paragraph:{pid}:MAIN", kind="contains"))


def test_batch_when_program_has_step_of_edge():
    """JCL-invoked program → Spring Batch."""
    s = NetworkXStore()
    _program(s, "PAYROLL")
    s.add_node(KGNode(id="job:J1", kind="job", name="J1", span=_span()))
    s.add_node(KGNode(id="step:J1:S1", kind="step", name="S1", span=_span()))
    s.add_edge(KGEdge(source="job:J1", target="step:J1:S1", kind="contains"))
    s.add_edge(KGEdge(source="program:PAYROLL", target="step:J1:S1", kind="step_of"))

    assert classify_program(s, "PAYROLL") == "batch"


def test_cics_when_program_contains_txn_call():
    """CICS-bound program → Spring MVC, even if JCL also invokes it."""
    s = NetworkXStore()
    _program(s, "CUSTINQ")
    s.add_node(
        KGNode(
            id="txn_call:CUSTINQ:0:5",
            kind="txn_call",
            name="CICS-LINK",
            span=_span(),
            attributes={"verb": "LINK"},
        )
    )
    s.add_edge(KGEdge(source="paragraph:CUSTINQ:MAIN", target="txn_call:CUSTINQ:0:5", kind="calls"))

    assert classify_program(s, "CUSTINQ") == "cics"


def test_cics_wins_over_batch_on_mixed_mode():
    """A program that's both JCL-invoked and CICS-bound classifies as cics.
    The REST surface is what matters — batch wrapping can come later.
    """
    s = NetworkXStore()
    _program(s, "MIXED")
    # JCL invocation
    s.add_node(KGNode(id="step:J:S", kind="step", name="S", span=_span()))
    s.add_edge(KGEdge(source="program:MIXED", target="step:J:S", kind="step_of"))
    # CICS txn
    s.add_node(
        KGNode(
            id="txn_call:MIXED:0:9",
            kind="txn_call",
            name="CICS-RECEIVE",
            span=_span(),
            attributes={"verb": "RECEIVE"},
        )
    )
    s.add_edge(KGEdge(source="paragraph:MIXED:MAIN", target="txn_call:MIXED:0:9", kind="calls"))

    assert classify_program(s, "MIXED") == "cics"


def test_subroutine_when_neither_jcl_nor_cics():
    """Program with only inbound CALL edges (a library) → @Service bean."""
    s = NetworkXStore()
    _program(s, "NOTIFYLIB")
    # Another program calls this one. The inbound edge doesn't make it
    # batch or cics.
    s.add_node(KGNode(id="program:CALLER", kind="program", name="CALLER", span=_span()))
    s.add_node(
        KGNode(
            id="external_call:CALLER:NOTIFYLIB:1",
            kind="external_call",
            name="NOTIFYLIB",
            span=_span(),
        )
    )
    s.add_edge(KGEdge(source="program:CALLER", target="external_call:CALLER:NOTIFYLIB:1", kind="calls"))

    assert classify_program(s, "NOTIFYLIB") == "subroutine"


def test_missing_program_raises():
    s = NetworkXStore()
    with pytest.raises(KeyError):
        classify_program(s, "DOES-NOT-EXIST")


def test_payroll_fixture_classifies_as_batch():
    """Integration check against the real PAYROLL fixture."""
    from pathlib import Path

    from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
        CARTRIDGE,
    )

    fixture = (
        Path(__file__).resolve().parents[1]
        / "cartridges"
        / "cobol_to_java25_springboot"
        / "corpus"
        / "fixtures"
        / "payroll"
    )
    s = NetworkXStore()
    adapter = CARTRIDGE.adapters()["cobol"]
    adapter.extract_kg(fixture, fixture / "PAYROLL.cbl", s)
    CARTRIDGE.ingest_kg_extras(fixture, s)

    # PAYROLL has step_of to STEP010, no EXEC CICS → batch.
    assert classify_program(s, "PAYROLL") == "batch"
