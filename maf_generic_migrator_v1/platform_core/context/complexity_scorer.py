"""Weighted-pattern complexity scorer.

The platform provides the scoring *driver*; the cartridge provides the pattern
table via ``complexity_patterns(language) -> list[(regex, weight, description)]``.

Falls back to a neutral size-based score if the cartridge supplies nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge
from maf_generic_migrator_v1.platform_core.ir import Inventory

Level = Literal["LOW", "MEDIUM", "HIGH"]


@dataclass
class ComplexityResult:
    score: int
    level: Level
    breakdown: dict[str, int] = field(default_factory=dict)


DEFAULT_THRESHOLDS = (5, 15)  # score < 5 => LOW, < 15 => MEDIUM, else HIGH


def score_file(
    path: Path,
    language: str,
    patterns: list[tuple[re.Pattern[str], int, str]] | None = None,
    thresholds: tuple[int, int] = DEFAULT_THRESHOLDS,
) -> ComplexityResult:
    text = path.read_text(encoding="utf-8", errors="replace")
    score, breakdown = _score_text(text, patterns or [])
    level = _to_level(score, text, thresholds)
    return ComplexityResult(score=score, level=level, breakdown=breakdown)


def score_inventory(inventory: Inventory, cartridge: MigrationCartridge) -> dict[str, Level]:
    """Compute a complexity level per unit using cartridge-supplied patterns."""
    fn = getattr(cartridge, "complexity_patterns", None)
    result: dict[str, Level] = {}
    repo_root = Path(inventory.repo_root)

    for unit in inventory.units:
        patterns = _compile(fn(unit.language)) if callable(fn) else []
        total = 0
        for rel in unit.files:
            p = repo_root / rel
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            s, _ = _score_text(text, patterns)
            total += s
        result[unit.unit_id] = _to_level(total, "\n" * unit.loc, DEFAULT_THRESHOLDS)
    return result


def _compile(raw: list[tuple[str, int, str]] | None) -> list[tuple[re.Pattern[str], int, str]]:
    if not raw:
        return []
    return [(re.compile(p), w, d) for p, w, d in raw]


def _score_text(text: str, patterns: list[tuple[re.Pattern[str], int, str]]) -> tuple[int, dict[str, int]]:
    if not patterns:
        lines = text.count("\n") + 1
        return (max(1, lines // 500), {"_fallback_lines": lines})

    score = 0
    breakdown: dict[str, int] = {}
    for rx, weight, desc in patterns:
        matches = len(rx.findall(text))
        if matches:
            score += matches * weight
            breakdown[desc] = breakdown.get(desc, 0) + matches * weight
    return score, breakdown


def _to_level(score: int, text: str, thresholds: tuple[int, int]) -> Level:
    low, high = thresholds
    if score >= high:
        return "HIGH"
    if score >= low:
        return "MEDIUM"
    return "LOW"
