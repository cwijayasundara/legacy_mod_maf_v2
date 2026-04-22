"""Smoke tests for the GnuCOBOL fixture recorder scaffold.

We don't require cobc in CI — the recorder must cleanly skip when it
isn't available. The parser + fixture-writer paths are tested in
isolation.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PILOT_TOOLS = Path(__file__).resolve().parents[2] / "pilot" / "tools"
sys.path.insert(0, str(PILOT_TOOLS))
import record_fixture  # noqa: E402


def test_key_value_parser_round_trip():
    out = record_fixture._parse_key_value_output(
        "OUT-EMP-ID=E00042\nOUT-GROSS=3600.00\n"
    )
    assert out == {"OUT-EMP-ID": "E00042", "OUT-GROSS": "3600.00"}


def test_key_value_parser_rejects_non_kv_output():
    assert record_fixture._parse_key_value_output("random text") is None


def test_key_value_parser_accepts_whitespace_and_empty_lines():
    out = record_fixture._parse_key_value_output(
        "\n  OUT=1  \n\n  OTHER = 2  \n"
    )
    assert out == {"OUT": "1", "OTHER": "2"}


def test_emit_fixture_writes_valid_dualrun_json(tmp_path: Path):
    path = record_fixture._emit_fixture(
        name="t1",
        target_style="subroutine",
        program_id="INTCALC",
        inputs={"LNK-BALANCE": "1000.00"},
        expected={"LNK-INTEREST": "50.00", "LNK-OK": "Y"},
        dest_dir=tmp_path,
        tolerances={"LNK-INTEREST": {"kind": "picture", "pic": "S9(7)V99"}},
    )
    data = json.loads(path.read_text())
    assert data["name"] == "t1"
    assert data["target_style"] == "subroutine"
    assert data["program_id"] == "INTCALC"
    assert data["expected_outputs"]["LNK-INTEREST"] == "50.00"
    assert data["metadata"]["captured_from"] == "GnuCOBOL"

    # And the output must be loadable through the real fixture loader.
    from maf_generic_migrator_v1.platform_core.dualrun import load_fixtures
    fixtures = load_fixtures(tmp_path)
    assert len(fixtures) == 1
    assert fixtures[0].tolerance_for("LNK-INTEREST").pic == "S9(7)V99"


def test_cli_exits_cleanly_when_cobc_missing(monkeypatch, tmp_path: Path):
    """If cobc isn't on PATH, the script must exit 1 with a clear error
    rather than trying to run a non-existent binary.
    """
    monkeypatch.setattr(record_fixture.shutil, "which", lambda _: None)
    rc = record_fixture.main(
        [
            "--program", str(tmp_path / "x.cbl"),
            "--name", "t",
            "--target-style", "subroutine",
            "--program-id", "X",
            "--input", "{}",
            "--out", str(tmp_path),
        ]
    )
    assert rc == 1


def test_cli_rejects_non_object_input(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(record_fixture.shutil, "which", lambda _: "/fake/cobc")
    rc = record_fixture.main(
        [
            "--program", str(tmp_path / "x.cbl"),
            "--name", "t",
            "--target-style", "subroutine",
            "--program-id", "X",
            "--input", "[1, 2, 3]",      # not an object
            "--out", str(tmp_path),
        ]
    )
    assert rc == 1


@pytest.mark.skipif(shutil.which("cobc") is None, reason="GnuCOBOL not installed")
def test_end_to_end_when_cobc_is_available(tmp_path: Path):
    """When cobc is actually available, compile and run a trivial
    program to prove the whole chain works.
    """
    src = tmp_path / "T.cbl"
    src.write_text(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. T.\n"
        "       DATA DIVISION.\n"
        "       WORKING-STORAGE SECTION.\n"
        "       01  INLINE       PIC X(20).\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN.\n"
        "           DISPLAY 'OUT=fine'.\n"
        "           STOP RUN.\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "fixtures"
    rc = record_fixture.main(
        [
            "--program", str(src),
            "--name", "trivial",
            "--target-style", "subroutine",
            "--program-id", "T",
            "--input", "{}",
            "--out", str(out_dir),
        ]
    )
    assert rc == 0
    assert (out_dir / "trivial.json").is_file()
