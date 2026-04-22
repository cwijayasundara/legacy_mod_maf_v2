"""Tests for the dualrun runners and the cartridge's verify_unit gate."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.dualrun import (
    DualRunFixture,
    NullRunner,
    SubprocessRunner,
    load_fixtures,
    run_dual_run,
)
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Contract

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "corpus"
    / "fixtures"
    / "payroll"
    / "dualrun"
)


def _item(unit_id: str = "PAYROLL") -> BacklogItem:
    return BacklogItem(
        unit_id=unit_id,
        cartridge_id=CARTRIDGE.id,
        wave=1,
        source_paths=[],
        contract=Contract(),
    )


# ---------------------------------------------------------------------- #
# Fixture loader
# ---------------------------------------------------------------------- #


def test_load_fixtures_from_payroll_dualrun_dir():
    fixtures = load_fixtures(FIXTURE_DIR)
    assert len(fixtures) >= 2
    names = {f.name for f in fixtures}
    assert "happy-path-monthly" in names
    assert "overtime-bonus-applies" in names
    # Tolerances parse correctly
    hp = next(f for f in fixtures if f.name == "happy-path-monthly")
    assert hp.tolerance_for("OUT-GROSS").pic == "9(5)V99"


def test_load_fixtures_missing_dir_returns_empty(tmp_path: Path):
    assert load_fixtures(tmp_path / "nope") == []


def test_load_fixtures_rejects_invalid_json(tmp_path: Path):
    (tmp_path / "bad.json").write_text("{ not json }", encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixtures(tmp_path)


# ---------------------------------------------------------------------- #
# NullRunner
# ---------------------------------------------------------------------- #


async def test_null_runner_skips_every_fixture():
    fixtures = load_fixtures(FIXTURE_DIR)
    runner = NullRunner(reason="dev-mode")
    verdict = await run_dual_run("PAYROLL", fixtures, runner)
    assert verdict.total == len(fixtures)
    assert verdict.skipped == len(fixtures)
    assert verdict.matched == 0
    assert verdict.mismatched == 0
    # All-skipped still passes — a dev without a JVM shouldn't block the pipeline.
    assert verdict.passed


# ---------------------------------------------------------------------- #
# SubprocessRunner
# ---------------------------------------------------------------------- #


async def test_subprocess_runner_roundtrips_json():
    """Spawn a Python subprocess that echoes inputs as outputs, then
    assert the fixture matches. Uses ``sys.executable`` so no installed
    binary is required.
    """
    # The helper just reads JSON from stdin and echoes the inputs it saw
    # under the keys the fixture expects.
    helper = (
        "import sys, json; "
        "data = json.loads(sys.stdin.read()); "
        "print(json.dumps({'OUT-EMP-ID': data['EMP-ID'], 'OUT-GROSS': '3600.00'}))"
    )

    runner = SubprocessRunner(
        command=[sys.executable, "-c", helper],
        timeout_s=10.0,
    )
    fixture = DualRunFixture(
        name="hp",
        target_style="batch",
        program_id="PAYROLL",
        inputs={"EMP-ID": "E00042", "EMP-HOURS": "160.00", "EMP-RATE": "22.50"},
        expected_outputs={"OUT-EMP-ID": "E00042", "OUT-GROSS": "3600.00"},
    )
    result = await runner.run(fixture)
    assert result.error is None
    assert result.skipped_reason is None
    assert result.outputs == {"OUT-EMP-ID": "E00042", "OUT-GROSS": "3600.00"}


async def test_subprocess_runner_non_json_stdout_is_error():
    runner = SubprocessRunner(
        command=[sys.executable, "-c", "print('hello, not json')"],
        timeout_s=10.0,
    )
    fixture = DualRunFixture(
        name="bad", target_style="batch", program_id="X", inputs={}, expected_outputs={},
    )
    result = await runner.run(fixture)
    assert result.error is not None
    assert "not JSON" in result.error


async def test_subprocess_runner_nonzero_exit_is_error():
    runner = SubprocessRunner(
        command=[sys.executable, "-c", "import sys; sys.exit(7)"],
        timeout_s=10.0,
    )
    fixture = DualRunFixture(
        name="bad", target_style="batch", program_id="X", inputs={}, expected_outputs={},
    )
    result = await runner.run(fixture)
    assert result.error is not None
    assert "exit=7" in result.error


async def test_subprocess_runner_missing_binary_skips():
    runner = SubprocessRunner(
        command=["definitely-not-a-real-binary-xyz-123"],
        timeout_s=5.0,
    )
    fixture = DualRunFixture(
        name="skip", target_style="batch", program_id="X", inputs={}, expected_outputs={},
    )
    result = await runner.run(fixture)
    assert result.skipped_reason is not None
    assert "not on PATH" in result.skipped_reason


async def test_subprocess_runner_interpolates_fixture_tokens(tmp_path: Path):
    """The argv template supports ``{program_id}`` / ``{fixture_name}``
    so cartridges can point the same command at many fixtures.
    """
    runner = SubprocessRunner(
        command=[
            sys.executable,
            "-c",
            # Emit the program_id + fixture_name as JSON to confirm they
            # made it into argv.
            "import sys, json; "
            "print(json.dumps({'pid': sys.argv[1], 'fn': sys.argv[2]}))",
            "{program_id}",
            "{fixture_name}",
        ],
        timeout_s=10.0,
    )
    fixture = DualRunFixture(
        name="my-fixture", target_style="batch", program_id="MYPROG",
        inputs={}, expected_outputs={"pid": "MYPROG", "fn": "my-fixture"},
    )
    result = await runner.run(fixture)
    assert result.outputs == {"pid": "MYPROG", "fn": "my-fixture"}


# ---------------------------------------------------------------------- #
# Cartridge verify_unit end-to-end
# ---------------------------------------------------------------------- #


def test_verify_unit_soft_passes_without_translator_output(tmp_path: Path):
    """No pom.xml yet → translator hasn't emitted anything → don't
    hard-fail at verify_unit (that's translator's problem, not the
    gate's).
    """
    assert CARTRIDGE.verify_unit(tmp_path, _item()) is True


def test_verify_unit_runs_dualrun_with_null_runner(tmp_path: Path, monkeypatch):
    """When Maven is missing AND no dualrun command is set, the gate
    must still write a verdict markdown (using NullRunner) rather than
    silently skipping.
    """
    # Simulate Maven missing so the mvn gate soft-passes.
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: None,
    )
    monkeypatch.delenv("LEGACY_MOD_DUALRUN_CMD", raising=False)
    monkeypatch.delenv("LEGACY_MOD_DUALRUN_REQUIRE", raising=False)

    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert CARTRIDGE.verify_unit(tmp_path, _item("PAYROLL")) is True
    verdict_md = (tmp_path / "dualrun_verdict.md").read_text(encoding="utf-8")
    assert "Dual-run verdict: PAYROLL" in verdict_md
    assert "SKIPPED" in verdict_md
    # The PAYROLL fixtures ship two recordings → both referenced here.
    assert "happy-path-monthly" in verdict_md
    assert "overtime-bonus-applies" in verdict_md


def test_verify_unit_require_flag_fails_on_all_skipped(tmp_path: Path, monkeypatch):
    """With LEGACY_MOD_DUALRUN_REQUIRE=1, skipped fixtures are no
    longer acceptable — the CI gate tightens.
    """
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: None,
    )
    monkeypatch.delenv("LEGACY_MOD_DUALRUN_CMD", raising=False)
    monkeypatch.setenv("LEGACY_MOD_DUALRUN_REQUIRE", "1")

    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert CARTRIDGE.verify_unit(tmp_path, _item("PAYROLL")) is False


def test_verify_unit_executes_dualrun_via_subprocess_runner(tmp_path: Path, monkeypatch):
    """When ``LEGACY_MOD_DUALRUN_CMD`` points at a real helper that
    returns the fixture's expected outputs, verify_unit must return
    True and the verdict must show PASS.
    """
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: None,
    )

    # Python helper that reads inputs on stdin and emits the fixture's
    # exact expected outputs on stdout. Same helper serves both fixtures.
    helper_code = (
        "import sys, json; "
        "data = json.loads(sys.stdin.read()); "
        "hours = float(data.get('EMP-HOURS', '0')); "
        "rate = float(data.get('EMP-RATE', '0')); "
        "gross = hours * rate + (50 if hours * rate > 1000 else 0); "
        "print(json.dumps({'OUT-EMP-ID': data['EMP-ID'], 'OUT-GROSS': f'{gross:.2f}'}))"
    )
    monkeypatch.setenv(
        "LEGACY_MOD_DUALRUN_CMD",
        f"{sys.executable} -c {json.dumps(helper_code)}",
    )
    monkeypatch.delenv("LEGACY_MOD_DUALRUN_REQUIRE", raising=False)

    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert CARTRIDGE.verify_unit(tmp_path, _item("PAYROLL")) is True

    verdict_md = (tmp_path / "dualrun_verdict.md").read_text(encoding="utf-8")
    assert "Overall: PASS" in verdict_md
    assert verdict_md.count("PASS") >= 3  # overall + 2 fixtures


def test_verify_unit_flags_mismatch_when_subprocess_returns_wrong_value(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: None,
    )
    # Helper that lies — always says gross is 0.
    bad_helper = (
        "import sys, json; "
        "data = json.loads(sys.stdin.read()); "
        "print(json.dumps({'OUT-EMP-ID': data['EMP-ID'], 'OUT-GROSS': '0.00'}))"
    )
    monkeypatch.setenv(
        "LEGACY_MOD_DUALRUN_CMD",
        f"{sys.executable} -c {json.dumps(bad_helper)}",
    )

    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    assert CARTRIDGE.verify_unit(tmp_path, _item("PAYROLL")) is False

    verdict_md = (tmp_path / "dualrun_verdict.md").read_text(encoding="utf-8")
    assert "Overall: FAIL" in verdict_md
    assert "OUT-GROSS" in verdict_md
