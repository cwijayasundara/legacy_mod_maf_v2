"""Tests for the upgraded translator data dictionary.

REDEFINES / OCCURS / 88-level / parent_group attributes must surface
in the table so translators can generate sealed interfaces, enums,
and @AssertTrue validators correctly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.translator import (
    _render_data_dictionary,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)


def _span() -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=1, end_line=1)


def _store_with_one_record() -> NetworkXStore:
    s = NetworkXStore()
    s.add_node(KGNode(id="program:P", kind="program", name="P", span=_span()))
    s.add_node(
        KGNode(
            id="record:P:REC",
            kind="record", name="REC", span=_span(),
            attributes={"level": "01"},
        )
    )
    s.add_edge(KGEdge(source="program:P", target="record:P:REC", kind="contains"))
    return s


def _add_field(
    store: NetworkXStore, record: str, name: str, **attrs: str
) -> None:
    store.add_node(
        KGNode(
            id=f"field:P:{record}:{name}",
            kind="field",
            name=name,
            span=_span(),
            attributes=dict(attrs),
        )
    )
    store.add_edge(
        KGEdge(source=f"record:P:{record}", target=f"field:P:{record}:{name}", kind="contains")
    )


def test_dictionary_columns_present():
    s = _store_with_one_record()
    _add_field(s, "REC", "F1", level="05", pic="X(8)")
    grid = _render_data_dictionary(s, "P")
    header = grid.splitlines()[0]
    for col in ("Record", "Field", "Level", "PIC", "REDEFINES", "OCCURS", "Conditions (88)", "Group"):
        assert col in header


def test_redefines_attribute_renders_in_its_column():
    s = _store_with_one_record()
    _add_field(s, "REC", "F1", level="05", pic="X(8)")
    _add_field(s, "REC", "F1-R", level="05", pic="X(8)", redefines="F1")
    grid = _render_data_dictionary(s, "P")
    assert "REDEFINES" in grid
    # F1-R row carries the REDEFINES target `F1`
    f1_r_row = next(line for line in grid.splitlines() if "`F1-R`" in line)
    assert "`F1`" in f1_r_row


def test_occurs_and_depending_on_render():
    s = _store_with_one_record()
    _add_field(s, "REC", "CNT", level="05", pic="9(3)")
    _add_field(
        s, "REC", "ITEM",
        level="05", pic="X(8)",
        occurs="1 TO 100 TIMES DEPENDING ON CNT",
        occurs_min="1", occurs_max="100", depending_on="CNT",
    )
    grid = _render_data_dictionary(s, "P")
    item_row = next(line for line in grid.splitlines() if "`ITEM`" in line)
    assert "DEPENDING ON CNT" in item_row


def test_condition_values_render():
    s = _store_with_one_record()
    _add_field(
        s, "REC", "STATUS",
        level="05", pic="X(1)",
        condition_values="ACTIVE='A'|SUSPENDED='S'|CLOSED='C'",
    )
    grid = _render_data_dictionary(s, "P")
    status_row = next(line for line in grid.splitlines() if "`STATUS`" in line)
    assert "ACTIVE='A'" in status_row
    assert "SUSPENDED='S'" in status_row


def test_parent_group_renders():
    s = _store_with_one_record()
    _add_field(s, "REC", "NAME-GROUP", level="05")
    _add_field(
        s, "REC", "NAME-FIRST",
        level="10", pic="X(20)", parent_group="NAME-GROUP",
    )
    grid = _render_data_dictionary(s, "P")
    child_row = next(line for line in grid.splitlines() if "`NAME-FIRST`" in line)
    assert "`NAME-GROUP`" in child_row


def test_empty_cells_show_em_dash():
    """Fields without REDEFINES/OCCURS/conditions/group must show `—`
    rather than the literal string ``None`` or a blank cell.
    """
    s = _store_with_one_record()
    _add_field(s, "REC", "PLAIN", level="05", pic="X(4)")
    grid = _render_data_dictionary(s, "P")
    row = next(line for line in grid.splitlines() if "`PLAIN`" in line)
    # Four empty cells (REDEFINES, OCCURS, Conditions, Group) each show —
    assert row.count("—") == 4


def test_empty_program_returns_empty_string():
    s = NetworkXStore()
    s.add_node(KGNode(id="program:P", kind="program", name="P", span=_span()))
    assert _render_data_dictionary(s, "P") == ""
