"""Tests for the COBOL adapter + KG extraction.

The cartridge ships a single golden fixture under ``corpus/fixtures/payroll/``
that exercises every node/edge kind we care about in Phase 1: sections,
paragraphs, FDs, records, fields, PERFORM, CALL, EXEC SQL, READ, WRITE.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.kg import NetworkXStore

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "corpus"
    / "fixtures"
    / "payroll"
)
PAYROLL_SRC = FIXTURE_ROOT / "PAYROLL.cbl"


@pytest.fixture()
def adapter():
    return CARTRIDGE.adapters()["cobol"]


@pytest.fixture()
def ir(adapter):
    return adapter.extract_unit(FIXTURE_ROOT, PAYROLL_SRC)


@pytest.fixture()
def store(adapter) -> NetworkXStore:
    s = NetworkXStore()
    adapter.extract_kg(FIXTURE_ROOT, PAYROLL_SRC, s)
    return s


# ---------------------------------------------------------------------- #
# UnitIR extraction
# ---------------------------------------------------------------------- #


def test_unit_id_is_program_id(ir):
    assert ir.unit_id == "PAYROLL"
    assert ir.language == "cobol"
    assert ir.kind == "module"
    assert ir.handler_entry == "PAYROLL:PROCEDURE DIVISION"


def test_service_calls_capture_cobol_call(ir):
    calls = [sc for sc in ir.service_calls if sc.service == "cobol-call"]
    assert len(calls) == 1
    assert calls[0].operation == "NOTIFYLIB"


def test_service_calls_capture_exec_sql_via_regex_fallback(ir):
    """tree-sitter-cobol drops EXEC SQL into ERROR nodes; regex catches it."""
    sql = [sc for sc in ir.service_calls if sc.service == "db2-exec-sql"]
    assert len(sql) == 1
    assert sql[0].operation == "INSERT"


def test_service_calls_capture_read_and_write(ir):
    vsam = [sc for sc in ir.service_calls if sc.service == "vsam"]
    ops = sorted(c.operation for c in vsam)
    assert "read:PAYROLL-FILE" in ops or "read:?" in ops
    assert any(op.startswith("write:") for op in ops)


# ---------------------------------------------------------------------- #
# KG — structural nodes
# ---------------------------------------------------------------------- #


def test_program_node_is_root(store: NetworkXStore):
    progs = list(store.iter_nodes(kind="program"))
    assert [p.name for p in progs] == ["PAYROLL"]


def test_every_paragraph_detected(store: NetworkXStore):
    """All six paragraphs must be detected even though EXEC SQL trips
    tree-sitter into ERROR mode. The regex union must close the gap.
    """
    names = sorted(n.name for n in store.iter_nodes(kind="paragraph"))
    assert names == sorted(
        [
            "OPEN-FILES",
            "PROCESS-RECORDS",
            "COMPUTE-GROSS",
            "OVERTIME-BONUS",
            "EMIT-ROW",
            "CLOSE-FILES",
        ]
    )


def test_scope_terminators_not_treated_as_paragraphs(store: NetworkXStore):
    names = {n.name for n in store.iter_nodes(kind="paragraph")}
    assert "END-READ" not in names
    assert "END-IF" not in names
    assert "END-EXEC" not in names


def test_sections_contain_their_paragraphs(store: NetworkXStore):
    """MAIN-SECTION must contain OPEN-FILES/PROCESS-RECORDS/COMPUTE-GROSS/
    OVERTIME-BONUS/CLOSE-FILES; WRITE-SECTION must contain EMIT-ROW.
    """
    main_children = {
        n.name
        for n in store.neighbors(
            "section:PAYROLL:MAIN-SECTION", direction="out", edge_kinds=["contains"]
        )
    }
    assert {"OPEN-FILES", "PROCESS-RECORDS", "COMPUTE-GROSS", "OVERTIME-BONUS"} <= main_children

    write_children = {
        n.name
        for n in store.neighbors(
            "section:PAYROLL:WRITE-SECTION", direction="out", edge_kinds=["contains"]
        )
    }
    assert "EMIT-ROW" in write_children


def test_data_division_file_record_field_hierarchy(store: NetworkXStore):
    files = {n.name for n in store.iter_nodes(kind="file")}
    assert files == {"PAYROLL-FILE", "OUTPUT-FILE"}

    payroll_records = {
        n.name
        for n in store.neighbors(
            "file:PAYROLL:PAYROLL-FILE", direction="out", edge_kinds=["contains"]
        )
    }
    assert payroll_records == {"PAYROLL-REC"}

    payroll_fields = {
        n.name
        for n in store.neighbors(
            "record:PAYROLL:PAYROLL-REC", direction="out", edge_kinds=["contains"]
        )
    }
    assert payroll_fields == {"EMP-ID", "EMP-HOURS", "EMP-RATE"}


def test_field_attributes_include_pic_clause(store: NetworkXStore):
    """PIC clauses (``PIC 9(3)V99 COMP-3``) must survive onto the field
    attributes — the translator will need them for BigDecimal scale/type.
    """
    emp_rate = store.get_node("field:PAYROLL:PAYROLL-REC:EMP-RATE")
    assert emp_rate is not None
    assert "pic" in emp_rate.attributes
    assert "9(3)V99" in emp_rate.attributes["pic"]


# ---------------------------------------------------------------------- #
# KG — procedural edges
# ---------------------------------------------------------------------- #


def test_perform_edges_root_at_correct_paragraph(store: NetworkXStore):
    targets = {
        e.target
        for e in store.iter_edges(
            source="paragraph:PAYROLL:PROCESS-RECORDS", kind="performs"
        )
    }
    # PROCESS-RECORDS performs COMPUTE-GROSS, OVERTIME-BONUS, EMIT-ROW
    # (and CLOSE-FILES via the AT END branch of the READ statement).
    expected = {
        "paragraph:PAYROLL:COMPUTE-GROSS",
        "paragraph:PAYROLL:OVERTIME-BONUS",
        "paragraph:PAYROLL:EMIT-ROW",
        "paragraph:PAYROLL:CLOSE-FILES",
    }
    assert expected <= targets


def test_call_edges_root_at_emit_row(store: NetworkXStore):
    calls = list(
        store.iter_edges(source="paragraph:PAYROLL:EMIT-ROW", kind="calls")
    )
    assert any("NOTIFYLIB" in e.target for e in calls)


def test_sql_block_edge_rooted_at_paragraph(store: NetworkXStore):
    """The SQL INSERT inside EMIT-ROW must produce a ``writes`` edge from
    the paragraph to the sql_block node — NOT from the program root.
    Pre-fix regression: edges were rooting at program:PAYROLL.
    """
    writes = list(
        store.iter_edges(source="paragraph:PAYROLL:EMIT-ROW", kind="writes")
    )
    assert any(e.target.startswith("sql_block:PAYROLL:") for e in writes)

    # And critically: NO writes edge from program root to that sql_block.
    prog_writes = list(
        store.iter_edges(source="program:PAYROLL", kind="writes")
    )
    assert not any(e.target.startswith("sql_block:") for e in prog_writes)


def test_write_edge_resolves_record_to_file(store: NetworkXStore):
    """WRITE takes a record name; we must resolve back to the enclosing FD."""
    writes = list(
        store.iter_edges(source="paragraph:PAYROLL:EMIT-ROW", kind="writes")
    )
    assert any(e.target == "file:PAYROLL:OUTPUT-FILE" for e in writes)


def test_read_edge_rooted_at_paragraph(store: NetworkXStore):
    """READ PAYROLL-FILE inside PROCESS-RECORDS must emit a paragraph->file
    ``reads`` edge.
    """
    reads = list(
        store.iter_edges(source="paragraph:PAYROLL:PROCESS-RECORDS", kind="reads")
    )
    assert any(e.target == "file:PAYROLL:PAYROLL-FILE" for e in reads)


# ---------------------------------------------------------------------- #
# Unit classifier
# ---------------------------------------------------------------------- #


def test_unit_classifier_finds_program_by_program_id():
    """The cartridge must pick up .cbl files with a PROGRAM-ID declaration."""
    units = CARTRIDGE.unit_classifier(FIXTURE_ROOT)
    assert len(units) == 1
    assert units[0].name == "PAYROLL.cbl"
