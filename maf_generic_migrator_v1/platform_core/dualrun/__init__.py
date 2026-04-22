"""Dual-run equivalence harness.

The Thoughtworks / Fowler methodology's gold-standard gate for mainframe
migrations: golden-fixture replay. Each ``DualRunFixture`` captures an
(inputs, expected_outputs) tuple from a reference run (mainframe,
GnuCOBOL, staging COBOL env). The harness drives the generated Java with
the same inputs and diffs actual vs expected with COBOL-aware
tolerances.

Three independent parts:

* ``fixtures`` — fixture model + loader.
* ``tolerance`` / ``diff`` — COBOL-aware field-level comparison.
* ``runner`` — abstract ``Runner`` + built-in ``SubprocessRunner`` and
  ``NullRunner`` — concrete execution is cartridge-pluggable.
* ``verdict`` — aggregates per-fixture diffs, renders Markdown reports.

High-level helper ``run_dual_run`` orchestrates: fixtures → runner →
diffs → verdict.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable

from .diff import FieldResult, FixtureDiff, diff_fixture
from .fixtures import DualRunFixture, TargetStyle, ToleranceKind, ToleranceSpec, load_fixtures
from .runner import NullRunner, Runner, RunnerOutput, SubprocessRunner
from .tolerance import FieldComparison, compare_field
from .verdict import Verdict, aggregate

__all__ = [
    "DualRunFixture",
    "FieldComparison",
    "FieldResult",
    "FixtureDiff",
    "NullRunner",
    "Runner",
    "RunnerOutput",
    "SubprocessRunner",
    "TargetStyle",
    "ToleranceKind",
    "ToleranceSpec",
    "Verdict",
    "aggregate",
    "compare_field",
    "diff_fixture",
    "load_fixtures",
    "run_dual_run",
]


async def run_dual_run(
    program_id: str,
    fixtures: Iterable[DualRunFixture],
    runner: Runner,
) -> Verdict:
    """Run every fixture through ``runner`` and aggregate a ``Verdict``.

    Runs fixtures sequentially — the assumption is that the runner hits
    one service at a time and parallelism complicates debug output. For
    large fixture suites a cartridge can wrap its own parallel driver.
    """
    diffs: list[FixtureDiff] = []
    for fixture in fixtures:
        result = await runner.run(fixture)
        diffs.append(
            diff_fixture(
                fixture,
                result.outputs,
                runner_skipped_reason=result.skipped_reason,
                runner_error=result.error,
            )
        )
    return aggregate(program_id, diffs)


async def run_dual_run_from_dir(
    program_id: str,
    fixtures_dir: Path,
    runner: Runner,
) -> Verdict:
    """Load fixtures from ``fixtures_dir`` and run them."""
    fixtures = load_fixtures(fixtures_dir)
    fixtures = [f for f in fixtures if f.program_id == program_id]
    return await run_dual_run(program_id, fixtures, runner)
