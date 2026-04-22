"""Deterministic token estimator and budget helpers.

Uses the chars/3 heuristic with a complexity multiplier, following the approach
from legacy_mod_maf_v1. Intentionally does not call any tokenizer — keep this
pure and synchronous so it can be used inside planner/chunker without a model
runtime dependency.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenProfile:
    name: str
    token_ceiling: int
    complexity_multipliers: dict[str, float]


DEFAULT_PROFILE = TokenProfile(
    name="default",
    token_ceiling=120_000,
    complexity_multipliers={"low": 1.5, "medium": 2.0, "high": 2.5},
)


def estimate_tokens(text: str, complexity: str = "medium", profile: TokenProfile = DEFAULT_PROFILE) -> int:
    base = len(text) / 3.0
    mult = profile.complexity_multipliers.get(complexity.lower(), 2.0)
    return int(base * mult)


def estimate_output_tokens(input_text: str, complexity: str = "medium") -> int:
    output_mult = {"low": 1.5, "medium": 2.0, "high": 3.0}.get(complexity.lower(), 2.0)
    return int(estimate_tokens(input_text, complexity) * output_mult)


def fits_in_context(text: str, complexity: str = "medium", profile: TokenProfile = DEFAULT_PROFILE) -> bool:
    return estimate_tokens(text, complexity, profile) <= profile.token_ceiling


def token_budget_remaining(used: int, profile: TokenProfile = DEFAULT_PROFILE) -> int:
    return max(0, profile.token_ceiling - used)
