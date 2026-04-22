"""Run cartridge rubrics against a unit's workdir and aggregate a score."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge, Rubric
from maf_generic_migrator_v1.platform_core.ir import BacklogItem


@dataclass
class RubricRowResult:
    rubric_id: str
    weight: float
    score: float
    error: str | None = None


@dataclass
class RubricReport:
    cartridge_id: str
    unit_id: str
    rows: list[RubricRowResult] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        total_weight = sum(r.weight for r in self.rows) or 1.0
        return sum(r.score * r.weight for r in self.rows) / total_weight

    def passed(self, threshold: float = 0.75) -> bool:
        return self.total_score >= threshold


def run_rubric(cartridge: MigrationCartridge, workdir: Path, item: BacklogItem) -> RubricReport:
    report = RubricReport(cartridge_id=cartridge.id, unit_id=item.unit_id)
    for rubric in cartridge.rubrics():
        row = _run_row(rubric, workdir, item)
        report.rows.append(row)
    return report


def _run_row(rubric: Rubric, workdir: Path, item: BacklogItem) -> RubricRowResult:
    fn = _resolve(rubric.scorer)
    if fn is None:
        return RubricRowResult(rubric_id=rubric.id, weight=rubric.weight, score=0.0, error=f"scorer not found: {rubric.scorer}")
    try:
        score = float(fn(workdir, item))
    except Exception as exc:  # noqa: BLE001
        return RubricRowResult(rubric_id=rubric.id, weight=rubric.weight, score=0.0, error=f"{type(exc).__name__}: {exc}")
    return RubricRowResult(rubric_id=rubric.id, weight=rubric.weight, score=max(0.0, min(1.0, score)))


def _resolve(dotted: str):
    mod, _, attr = dotted.rpartition(".")
    if not mod:
        return None
    try:
        module = importlib.import_module(mod)
    except ImportError:
        return None
    return getattr(module, attr, None)
