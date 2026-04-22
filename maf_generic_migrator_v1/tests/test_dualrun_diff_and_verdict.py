"""Tests for the diff engine and verdict aggregation."""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.dualrun import (
    DualRunFixture,
    ToleranceSpec,
    aggregate,
    diff_fixture,
)


def _fixture(expected: dict, tolerances: dict | None = None) -> DualRunFixture:
    return DualRunFixture(
        name="t",
        target_style="batch",
        program_id="PROG",
        inputs={},
        expected_outputs=expected,
        tolerances=tolerances or {},
    )


# ---------------------------------------------------------------------- #
# Per-fixture diff
# ---------------------------------------------------------------------- #


def test_all_fields_match_produces_matched_diff():
    fixture = _fixture({"A": "x", "B": "1.00"}, {"B": ToleranceSpec(kind="decimal", scale=2)})
    diff = diff_fixture(fixture, {"A": "x", "B": "1"})
    assert diff.matched is True
    assert diff.total_field_count == 2
    assert diff.matched_field_count == 2


def test_missing_field_in_actual_is_a_mismatch():
    diff = diff_fixture(_fixture({"A": "x"}), {})
    assert not diff.matched
    assert diff.fields[0].reason == "missing from actual outputs"
    assert diff.fields[0].present_in_actual is False


def test_extra_field_in_actual_is_flagged():
    """A field in the actual output that isn't declared in the fixture
    is a potential regression (translator hallucination or expanded API).
    """
    diff = diff_fixture(_fixture({"A": "x"}), {"A": "x", "SURPRISE": "boo"})
    assert diff.extra_fields == ["SURPRISE"]
    assert not diff.matched  # extra fields fail the diff


def test_runner_skip_is_recorded_without_comparisons():
    diff = diff_fixture(
        _fixture({"A": "x"}),
        actual_outputs=None,
        runner_skipped_reason="no JVM",
    )
    assert diff.runner_skipped_reason == "no JVM"
    assert diff.fields == []
    assert not diff.matched


def test_runner_error_is_recorded_without_comparisons():
    diff = diff_fixture(
        _fixture({"A": "x"}),
        actual_outputs=None,
        runner_error="exit=127",
    )
    assert diff.runner_error == "exit=127"
    assert not diff.matched


# ---------------------------------------------------------------------- #
# Verdict aggregation
# ---------------------------------------------------------------------- #


def test_verdict_counts_each_category():
    fixtures = [_fixture({"A": "x"}) for _ in range(4)]
    diffs = [
        diff_fixture(fixtures[0], {"A": "x"}),                                   # match
        diff_fixture(fixtures[1], {"A": "y"}),                                   # mismatch
        diff_fixture(fixtures[2], None, runner_skipped_reason="no JVM"),         # skip
        diff_fixture(fixtures[3], None, runner_error="boom"),                    # error
    ]
    verdict = aggregate("PROG", diffs)
    assert verdict.total == 4
    assert verdict.matched == 1
    assert verdict.mismatched == 1
    assert verdict.skipped == 1
    assert verdict.errored == 1
    assert not verdict.passed      # any mismatch / error / 0-total fails


def test_verdict_passes_when_every_fixture_matched_or_skipped():
    fixtures = [_fixture({"A": "x"}) for _ in range(2)]
    diffs = [
        diff_fixture(fixtures[0], {"A": "x"}),
        diff_fixture(fixtures[1], None, runner_skipped_reason="no JVM"),
    ]
    assert aggregate("PROG", diffs).passed


def test_verdict_fails_when_all_skipped_and_none_executed():
    """Zero-total verdict doesn't count as a pass — callers expect
    ``passed`` to mean "I affirmed something". A run with no fixtures
    is ``passed == False`` so dashboards notice the gap.
    """
    verdict = aggregate("PROG", [])
    assert not verdict.passed
    assert verdict.total == 0


# ---------------------------------------------------------------------- #
# Markdown rendering
# ---------------------------------------------------------------------- #


def test_markdown_reports_mismatch_field_table():
    fixture = _fixture({"A": "x", "B": "1.00"}, {"B": ToleranceSpec(kind="decimal", scale=2)})
    diff = diff_fixture(fixture, {"A": "y", "B": "1.001"})
    md = aggregate("PROG", [diff]).render_markdown()
    assert "PROG" in md
    assert "FAIL" in md
    assert "Field mismatches" in md
    assert "`A`" in md
    assert "`B`" in md


def test_markdown_reports_skipped_reason():
    diff = diff_fixture(_fixture({"A": "x"}), None, runner_skipped_reason="no JVM")
    md = aggregate("PROG", [diff]).render_markdown()
    assert "SKIPPED" in md
    assert "no JVM" in md


def test_markdown_for_all_pass():
    diff = diff_fixture(_fixture({"A": "x"}), {"A": "x"})
    md = aggregate("PROG", [diff]).render_markdown()
    assert "PASS" in md
    assert "Field mismatches" not in md        # only shown when there are mismatches
