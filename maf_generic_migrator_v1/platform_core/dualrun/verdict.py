"""Dual-run verdict aggregation and Markdown rendering.

Turns a list of per-fixture diffs into one ``Verdict`` the cartridge's
``verify_unit`` can pass/fail on, plus a human-readable Markdown report
for attaching to the unit workdir.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .diff import FixtureDiff


@dataclass
class Verdict:
    """Aggregate of every fixture's diff for one unit."""

    program_id: str
    diffs: list[FixtureDiff]

    @property
    def total(self) -> int:
        return len(self.diffs)

    @property
    def matched(self) -> int:
        return sum(1 for d in self.diffs if d.matched)

    @property
    def mismatched(self) -> int:
        return sum(
            1
            for d in self.diffs
            if not d.matched and d.runner_skipped_reason is None and d.runner_error is None
        )

    @property
    def skipped(self) -> int:
        return sum(1 for d in self.diffs if d.runner_skipped_reason is not None)

    @property
    def errored(self) -> int:
        return sum(1 for d in self.diffs if d.runner_error is not None)

    @property
    def passed(self) -> bool:
        """``True`` when every fixture matched or was explicitly skipped.

        Errored fixtures always fail. Skipped fixtures don't — a dev
        without a JVM on their laptop shouldn't block progress.
        """
        return self.errored == 0 and self.mismatched == 0 and self.total > 0

    def render_markdown(self) -> str:
        if not self.diffs:
            return f"# Dual-run verdict: {self.program_id}\n\n(no fixtures)\n"

        lines: list[str] = [
            f"# Dual-run verdict: {self.program_id}",
            "",
            f"- Fixtures: {self.total}",
            f"- Matched: {self.matched}",
            f"- Mismatched: {self.mismatched}",
            f"- Skipped: {self.skipped}",
            f"- Errored: {self.errored}",
            f"- **Overall: {'PASS' if self.passed else 'FAIL'}**",
            "",
        ]

        for diff in self.diffs:
            status = _status_label(diff)
            lines.append(f"## {diff.fixture_name} — {status}")
            if diff.runner_skipped_reason:
                lines.append(f"_skipped: {diff.runner_skipped_reason}_")
                lines.append("")
                continue
            if diff.runner_error:
                lines.append(f"_runner error: {diff.runner_error}_")
                lines.append("")
                continue
            lines.append(f"- Target style: `{diff.target_style}`")
            lines.append(f"- Fields matched: {diff.matched_field_count}/{diff.total_field_count}")
            mismatches = [f for f in diff.fields if not f.matched]
            if mismatches:
                lines.append("")
                lines.append("### Field mismatches")
                lines.append("")
                lines.append("| Field | Expected | Actual | Reason |")
                lines.append("| --- | --- | --- | --- |")
                for fr in mismatches:
                    lines.append(
                        f"| `{fr.field}` | `{fr.expected}` | `{fr.actual}` | {fr.reason} |"
                    )
            if diff.extra_fields:
                lines.append("")
                lines.append(
                    "### Extra fields in actual (not in fixture): "
                    + ", ".join(f"`{f}`" for f in diff.extra_fields)
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _status_label(diff: FixtureDiff) -> str:
    if diff.runner_skipped_reason:
        return "SKIPPED"
    if diff.runner_error:
        return "ERROR"
    return "PASS" if diff.matched else "FAIL"


def aggregate(program_id: str, diffs: Iterable[FixtureDiff]) -> Verdict:
    """Build a ``Verdict`` from an iterable of diffs."""
    return Verdict(program_id=program_id, diffs=list(diffs))


__all__ = ["Verdict", "aggregate"]
