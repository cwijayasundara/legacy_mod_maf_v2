"""Smoke test for the pilot runner.

Runs ``pilot/run_pilot.py`` end-to-end and asserts the key invariants:
all three corpus programs are discovered, each gets a distinct target
style (cics / batch / subroutine), each gets a business spec, each
emits at least one generated file, and no stage fatally errored.

Keeps the pilot green as the platform evolves. Not a replacement for
per-module unit tests; this is the integration signal.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

PILOT_DIR = Path(__file__).resolve().parents[2] / "pilot"


@pytest.fixture(scope="module")
def pilot_result(tmp_path_factory):
    """Import and run the pilot once for the whole module.

    Redirects ``OUTPUT_ROOT`` to a tmpdir so an ad-hoc ``python
    pilot/run_pilot.py`` workspace under ``pilot/output/`` (if present)
    isn't stomped by the test run. Nothing under ``pilot/output/`` is
    tracked in git; it's regenerated on demand.
    """
    # Import lazily; pilot/run_pilot.py isn't under the package path.
    sys.path.insert(0, str(PILOT_DIR))
    import run_pilot

    run_pilot.OUTPUT_ROOT = tmp_path_factory.mktemp("pilot_out")
    summary = asyncio.run(run_pilot.run_pilot())
    return summary, run_pilot.OUTPUT_ROOT


def test_no_fatal_error(pilot_result):
    summary, _ = pilot_result
    assert summary.fatal == "", f"pilot fatal: {summary.fatal}"


def test_all_three_programs_discovered(pilot_result):
    summary, _ = pilot_result
    assert len(summary.programs) == 3
    pids = {p.program_id for p in summary.programs}
    assert pids == {"CUSTINQ", "INTCALC", "STMTGEN"}


def test_each_program_classified_distinctly(pilot_result):
    """The corpus was built so every target style fires exactly once:
    CUSTINQ → cics (EXEC CICS), INTCALC → subroutine (LINKAGE USING),
    STMTGEN → batch (JCL step, no LINKAGE).
    """
    summary, _ = pilot_result
    style_by_pid = {p.program_id: p.target_style for p in summary.programs}
    assert style_by_pid == {"CUSTINQ": "cics", "INTCALC": "subroutine", "STMTGEN": "batch"}


def test_every_program_emitted_a_maven_module(pilot_result):
    summary, output_root = pilot_result
    for program in summary.programs:
        assert program.files_written, f"{program.program_id} produced no output"
        assert "pom.xml" in program.files_written
        # Nested Maven layout survived the fenced-block round-trip.
        assert any(
            f.startswith(f"src/main/java/com/example/{program.program_id}/")
            for f in program.files_written
        ), f"{program.program_id} missing src/main/java tree"


def test_each_program_has_business_spec(pilot_result):
    summary, output_root = pilot_result
    for program in summary.programs:
        spec_path = output_root / "business_specs" / f"{program.program_id}.md"
        assert spec_path.is_file(), f"missing spec: {spec_path}"
        text = spec_path.read_text(encoding="utf-8")
        # Every spec must include at least these sections.
        assert "## Purpose" in text
        assert "## Inputs" in text
        assert "## Outputs" in text
        assert "## Side effects" in text


def test_cics_and_subroutine_specs_exclude_jcl_touches_only(pilot_result):
    """Regression guard for the classification-aware filter: INTCALC has
    a JCL wrapper (STEP020) that binds it to PROD.INT.ACCRUAL, but the
    subroutine's logical inputs are its LINKAGE SECTION — not those
    datasets. The business-spec filter must exclude touches_only.
    """
    _, output_root = pilot_result
    intcalc_spec = (output_root / "business_specs" / "INTCALC.md").read_text(encoding="utf-8")
    assert "PROD.INT.ACCRUAL" not in intcalc_spec
    assert "PROD.STMT.DAILY" not in intcalc_spec
    # The Inputs section should be "(none)" for this subroutine.
    purpose_to_inputs = intcalc_spec.split("## Inputs", 1)[1].split("## Outputs", 1)[0]
    assert "(none)" in purpose_to_inputs


def test_kg_node_counts_cover_expected_kinds(pilot_result):
    """Sanity check: the corpus exercises every adapter pathway we care
    about — SQL blocks (EXEC SQL), CICS (EXEC CICS), external calls
    (CALL), file I/O (READ/WRITE), JCL steps, and nested contains.
    """
    summary, _ = pilot_result
    required_kinds = {
        "program", "section", "paragraph", "field",
        "sql_block", "txn_call", "external_call",
        "file", "record", "dataset_ref", "job", "step",
    }
    present = set(summary.kg_node_counts)
    missing = required_kinds - present
    assert not missing, f"pilot KG missing node kinds: {missing}"


def test_verify_unit_reports_something_for_every_program(pilot_result):
    summary, _ = pilot_result
    for program in summary.programs:
        # Currently soft-passes (mvn missing, NullRunner for dualrun) —
        # the invariant we care about is that verify_unit didn't throw.
        assert program.verify_unit_passed is True


def test_pilot_summary_persists_to_disk(pilot_result):
    _, output_root = pilot_result
    summary_json = (output_root / "pilot_summary.json").read_text(encoding="utf-8")
    parsed = json.loads(summary_json)
    assert parsed["fatal"] == ""
    assert len(parsed["programs"]) == 3
