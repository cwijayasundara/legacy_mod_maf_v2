"""Field-by-field diff of expected vs actual outputs.

Takes a loaded fixture + the ``actual`` dict produced by the runner and
emits a ``FixtureDiff`` with one ``FieldResult`` per expected field
plus flags for fields present in the actual but not declared.

The diff is deterministic and side-effect free — rendering + I/O live in
``verdict.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fixtures import DualRunFixture
from .tolerance import FieldComparison, compare_field


@dataclass(frozen=True)
class FieldResult:
    """One per-field outcome."""

    field: str
    matched: bool
    reason: str
    expected: str
    actual: str
    present_in_actual: bool


@dataclass
class FixtureDiff:
    """Per-fixture diff summary."""

    fixture_name: str
    program_id: str
    target_style: str
    fields: list[FieldResult] = field(default_factory=list)
    extra_fields: list[str] = field(default_factory=list)
    runner_skipped_reason: str | None = None
    runner_error: str | None = None

    @property
    def matched(self) -> bool:
        if self.runner_skipped_reason is not None or self.runner_error is not None:
            return False
        return all(f.matched for f in self.fields) and not self.extra_fields

    @property
    def matched_field_count(self) -> int:
        return sum(1 for f in self.fields if f.matched)

    @property
    def total_field_count(self) -> int:
        return len(self.fields)


def diff_fixture(
    fixture: DualRunFixture,
    actual_outputs: dict[str, Any] | None,
    *,
    runner_skipped_reason: str | None = None,
    runner_error: str | None = None,
) -> FixtureDiff:
    """Compare ``actual_outputs`` against ``fixture.expected_outputs``.

    Passing ``actual_outputs=None`` with a ``runner_skipped_reason`` or
    ``runner_error`` records the condition without running any
    comparisons — useful when the runner couldn't execute (no JVM on
    the machine, target service down, etc).
    """
    diff = FixtureDiff(
        fixture_name=fixture.name,
        program_id=fixture.program_id,
        target_style=fixture.target_style,
        runner_skipped_reason=runner_skipped_reason,
        runner_error=runner_error,
    )
    if actual_outputs is None:
        return diff

    actual = dict(actual_outputs)
    for name, expected_value in fixture.expected_outputs.items():
        if name not in actual:
            diff.fields.append(
                FieldResult(
                    field=name,
                    matched=False,
                    reason="missing from actual outputs",
                    expected=str(expected_value),
                    actual="",
                    present_in_actual=False,
                )
            )
            continue
        comparison = compare_field(expected_value, actual[name], fixture.tolerance_for(name))
        diff.fields.append(
            FieldResult(
                field=name,
                matched=comparison.matched,
                reason=comparison.reason,
                expected=comparison.normalized_expected,
                actual=comparison.normalized_actual,
                present_in_actual=True,
            )
        )
        actual.pop(name)

    # Any fields left in ``actual`` are undeclared — potentially a translator
    # regression (it's emitting fields the mainframe never did).
    diff.extra_fields = sorted(actual.keys())
    return diff


__all__ = ["FieldResult", "FixtureDiff", "diff_fixture"]
