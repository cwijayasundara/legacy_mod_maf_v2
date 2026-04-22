"""Tests for Phase 6 adapter upgrades: REDEFINES, 88-levels, OCCURS,
copybook expansion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.adapters.cobol import (
    CobolAdapter,
)
from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.kg import NetworkXStore

PILOT_CORPUS = Path(__file__).resolve().parents[2] / "pilot" / "corpus"


# ---------------------------------------------------------------------- #
# Copybook expansion + WORKING-STORAGE extraction
# ---------------------------------------------------------------------- #


def _build_store_for(program_stem: str) -> NetworkXStore:
    adapter = CARTRIDGE.adapters()["cobol"]
    store = NetworkXStore()
    source = PILOT_CORPUS / "programs" / f"{program_stem}.cbl"
    adapter.extract_kg(PILOT_CORPUS, source, store)
    return store


def test_custinq_copybook_is_expanded():
    """CUSTINQ.cbl has `COPY CUSTREC.` which should materialize the
    CUSTOMER-REC record plus all its fields into the KG.
    """
    store = _build_store_for("CUSTINQ")
    records = {n.name for n in store.iter_nodes(kind="record")}
    assert "CUSTOMER-REC" in records

    fields = {n.name for n in store.iter_nodes(kind="field")}
    for expected in ("CUST-ID", "CUST-FIRST", "CUST-LAST", "CUST-BAL", "CUST-STATUS"):
        assert expected in fields, f"copybook field missing: {expected}"


def test_stmtgen_sees_copybook_too():
    """Same copybook, different program — both should independently
    ingest the fields.
    """
    store = _build_store_for("STMTGEN")
    fields = {n.name for n in store.iter_nodes(kind="field")}
    assert "CUST-BAL" in fields
    assert "CUST-RATE" in fields


def test_missing_copybook_does_not_crash(tmp_path: Path):
    """If a COPY references a copybook that can't be resolved, the
    adapter leaves the directive in place and keeps going — the UnitIR
    still records the COPY as an import.
    """
    source = tmp_path / "MISSING.cbl"
    source.write_text(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. MISSING.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       COPY NOSUCHCOPYBOOK.\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN-SECTION SECTION.\n"
        "       MAIN-PARA.\n"
        "           DISPLAY 'hi'.\n"
        "           GOBACK.\n"
    )
    store = NetworkXStore()
    CobolAdapter().extract_kg(tmp_path, source, store)
    ir = CobolAdapter().extract_unit(tmp_path, source)
    # UnitIR still records the COPY dependency
    assert any(imp.module == "NOSUCHCOPYBOOK" for imp in ir.imports)
    # But no phantom field/record nodes
    assert list(store.iter_nodes(kind="record")) == []


# ---------------------------------------------------------------------- #
# REDEFINES
# ---------------------------------------------------------------------- #


def test_field_level_redefines_emits_edge():
    """CUSTREC.cpy: CUST-EXTRA-R REDEFINES CUST-EXTRA. Expected: a
    ``redefines`` edge from the redefining field to the original.
    """
    store = _build_store_for("CUSTINQ")
    edges = list(store.iter_edges(kind="redefines"))
    assert any(
        e.source.endswith(":CUST-EXTRA-R") and e.target.endswith(":CUST-EXTRA")
        for e in edges
    ), f"no field-level REDEFINES edge: {edges}"


def test_redefines_target_stored_on_attributes():
    """The redefining node must also carry the target in attributes so
    translators can read it without walking edges.
    """
    store = _build_store_for("CUSTINQ")
    node = store.get_node("field:CUSTINQ:CUSTOMER-REC:CUST-EXTRA-R")
    assert node is not None
    assert node.attributes.get("redefines") == "CUST-EXTRA"


def test_record_level_redefines_edge(tmp_path: Path):
    """Record-level REDEFINES (01 WS-R REDEFINES WS-ITEMS) produces a
    record→record edge.
    """
    source = tmp_path / "RECRED.cbl"
    source.write_text(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. RECRED.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-ITEMS.\n"
        "           05  ITEM-ID   PIC X(8).\n"
        "       01  WS-ITEMS-R REDEFINES WS-ITEMS.\n"
        "           05  WS-BUFFER PIC X(8).\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN-SECTION SECTION.\n"
        "       MAIN.\n"
        "           GOBACK.\n"
    )
    store = NetworkXStore()
    CobolAdapter().extract_kg(tmp_path, source, store)
    edges = list(store.iter_edges(kind="redefines"))
    assert any(
        e.source == "record:RECRED:WS-ITEMS-R" and e.target == "record:RECRED:WS-ITEMS"
        for e in edges
    )


# ---------------------------------------------------------------------- #
# 88-level condition names
# ---------------------------------------------------------------------- #


def test_cust_status_condition_values_attached_to_parent():
    """CUSTREC: CUST-STATUS has 3 88-levels (ACTIVE/SUSPENDED/CLOSED).
    They must end up on CUST-STATUS as a pipe-joined list of name=value
    entries — not as separate field nodes.
    """
    store = _build_store_for("CUSTINQ")

    cust_status = store.get_node("field:CUSTINQ:CUSTOMER-REC:CUST-STATUS")
    assert cust_status is not None
    cv = cust_status.attributes.get("condition_values", "")
    assert "ACTIVE='A'" in cv
    assert "SUSPENDED='S'" in cv
    assert "CLOSED='C'" in cv


def test_88_levels_not_emitted_as_separate_fields():
    """Regression guard: 88-levels used to show up as fields named
    ACTIVE / SUSPENDED / CLOSED. They mustn't.
    """
    store = _build_store_for("CUSTINQ")
    field_names = {n.name for n in store.iter_nodes(kind="field")}
    for forbidden in ("ACTIVE", "SUSPENDED", "CLOSED"):
        assert forbidden not in field_names


# ---------------------------------------------------------------------- #
# OCCURS (incl. DEPENDING ON)
# ---------------------------------------------------------------------- #


def test_occurs_fixed_captured(tmp_path: Path):
    source = tmp_path / "OCC1.cbl"
    source.write_text(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. OCC1.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-TBL.\n"
        "           05  ROW   OCCURS 10 TIMES.\n"
        "               10  ROW-ID PIC X(4).\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN-SECTION SECTION.\n"
        "       MAIN.\n"
        "           GOBACK.\n"
    )
    store = NetworkXStore()
    CobolAdapter().extract_kg(tmp_path, source, store)
    row = store.get_node("field:OCC1:WS-TBL:ROW")
    assert row is not None
    assert row.attributes.get("occurs_min") == "10"
    assert row.attributes.get("occurs_max") == "10"
    assert row.attributes.get("occurs") == "10 TIMES"


def test_occurs_depending_on_captures_field_name(tmp_path: Path):
    source = tmp_path / "OCC2.cbl"
    source.write_text(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. OCC2.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  WS-DYN.\n"
        "           05  ENTRY-COUNT PIC 9(3).\n"
        "           05  ENTRY-ITEM  OCCURS 1 TO 100 TIMES\n"
        "                           DEPENDING ON ENTRY-COUNT.\n"
        "               10  ITEM-VAL PIC X(8).\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN-SECTION SECTION.\n"
        "       MAIN.\n"
        "           GOBACK.\n"
    )
    store = NetworkXStore()
    CobolAdapter().extract_kg(tmp_path, source, store)
    entry = store.get_node("field:OCC2:WS-DYN:ENTRY-ITEM")
    assert entry is not None
    assert entry.attributes.get("occurs_min") == "1"
    assert entry.attributes.get("occurs_max") == "100"
    assert entry.attributes.get("depending_on") == "ENTRY-COUNT"
    assert "DEPENDING ON ENTRY-COUNT" in entry.attributes.get("occurs", "")


# ---------------------------------------------------------------------- #
# parent_group nesting
# ---------------------------------------------------------------------- #


def test_nested_group_captures_parent_group_attr():
    """CUSTOMER-REC.CUST-NAME has two level-10 children: CUST-FIRST and
    CUST-LAST. Their ``parent_group`` attribute must name CUST-NAME.
    """
    store = _build_store_for("CUSTINQ")
    first = store.get_node("field:CUSTINQ:CUSTOMER-REC:CUST-FIRST")
    last = store.get_node("field:CUSTINQ:CUSTOMER-REC:CUST-LAST")
    assert first is not None and last is not None
    assert first.attributes.get("parent_group") == "CUST-NAME"
    assert last.attributes.get("parent_group") == "CUST-NAME"


def test_top_level_field_has_no_parent_group():
    """CUST-ID is directly under the 01 record — no parent group."""
    store = _build_store_for("CUSTINQ")
    cust_id = store.get_node("field:CUSTINQ:CUSTOMER-REC:CUST-ID")
    assert cust_id is not None
    assert "parent_group" not in cust_id.attributes
