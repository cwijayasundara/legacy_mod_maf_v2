"""USD budget enforcement for LLM calls.

Tracks input/output tokens reported by each agent response, multiplies by a
per-model rate table, and halts the run once ``max_budget_usd`` is reached.

Used via a ``ContextVar`` so the tracker doesn't need to be plumbed through
every helper signature:

    from .budget import BudgetTracker, set_current, current

    tracker = BudgetTracker(max_usd=10.0)
    set_current(tracker)
    try:
        await _run_impl(...)
    finally:
        logger.info(tracker.summary())
        set_current(None)

Pricing is best-effort. Unknown models fall back to a conservative mid-range
rate so "silent zero cost" never happens for a model we don't recognize.
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# USD per 1M tokens, (input, output). Values are list-price, current as of
# the knowledge cutoff; update when you re-negotiate. Keys are lowercase
# model IDs matched against the deployment/model string reported by the
# provider (case-insensitive exact match, then prefix match).
PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5.4-mini":    (0.15, 0.60),
    "gpt-5":           (2.50, 10.00),
    "gpt-4o-mini":     (0.15, 0.60),
    "gpt-4o":          (2.50, 10.00),
    "gpt-4.1-mini":    (0.40, 1.60),
    "gpt-4.1":         (2.00, 8.00),
    # Anthropic
    "claude-opus-4-7":    (15.00, 75.00),
    "claude-sonnet-4-6":  (3.00, 15.00),
    "claude-sonnet-4-5":  (3.00, 15.00),
    "claude-haiku-4-5":   (1.00, 5.00),
}

# Fallback rates when the model is unknown: deliberately NOT zero, so we
# don't accidentally hand out a free pass. Use a mid-tier rate.
_FALLBACK_RATES = (1.00, 5.00)

# Local-inference model name prefixes — inference cost is $0 because the
# user is running the model themselves (Ollama, LM Studio, llama.cpp, vLLM).
# Matches Ollama's tag scheme where the base name precedes `:tag` or `.size`.
_LOCAL_PREFIXES: tuple[str, ...] = (
    "qwen",        # qwen, qwen2, qwen2.5, qwen3, qwen3.6 (Ollama tags)
    "llama",       # llama3, llama3.1, llama3.2
    "gemma",       # gemma, gemma2, gemma3
    "phi",         # phi3, phi4
    "mistral",
    "mixtral",
    "codellama",
    "codegemma",
    "deepseek",    # deepseek-coder, deepseek-v3
    "starcoder",
    "yi:",
    "nomic-",
)


def _resolve_rates(model: str | None) -> tuple[float, float]:
    if not model:
        return _FALLBACK_RATES
    key = model.lower()
    # Exact match on paid model IDs first.
    if key in PRICING:
        return PRICING[key]
    # Prefix match against paid model IDs (handles "gpt-5.4-mini-2025-10-01").
    for candidate, rates in PRICING.items():
        if key.startswith(candidate):
            return rates
    # Local inference — free.
    for prefix in _LOCAL_PREFIXES:
        if key.startswith(prefix):
            return (0.0, 0.0)
    return _FALLBACK_RATES


@dataclass
class BudgetTracker:
    """Accumulates LLM cost across the run; halts when ``max_usd`` is reached."""

    max_usd: float
    used_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0
    halted: bool = False
    by_model: dict[str, dict] = field(default_factory=dict)
    by_tag: dict[str, int] = field(default_factory=dict)

    def cost_of(self, *, model: str | None, in_tokens: int, out_tokens: int) -> float:
        """Compute USD cost for a single call without mutating state."""
        rates_in, rates_out = _resolve_rates(model)
        return (in_tokens / 1_000_000) * rates_in + (out_tokens / 1_000_000) * rates_out

    def record(self, *, model: str | None, in_tokens: int, out_tokens: int, tag: str = "") -> None:
        rates_in, rates_out = _resolve_rates(model)
        delta_usd = (in_tokens / 1_000_000) * rates_in + (out_tokens / 1_000_000) * rates_out
        self.used_usd += delta_usd
        self.input_tokens += in_tokens
        self.output_tokens += out_tokens
        self.call_count += 1

        if tag:
            self.by_tag[tag] = self.by_tag.get(tag, 0) + 1

        model_key = (model or "unknown").lower()
        bucket = self.by_model.setdefault(
            model_key, {"calls": 0, "in": 0, "out": 0, "usd": 0.0}
        )
        bucket["calls"] += 1
        bucket["in"] += in_tokens
        bucket["out"] += out_tokens
        bucket["usd"] += delta_usd

        if self.used_usd >= self.max_usd and not self.halted:
            self.halted = True
            logger.error(
                "LLM budget exhausted: $%.4f >= $%.2f after %d calls — "
                "subsequent LLM calls will be skipped",
                self.used_usd,
                self.max_usd,
                self.call_count,
            )

    @property
    def should_halt(self) -> bool:
        return self.halted

    def summary(self) -> str:
        per_model = "; ".join(
            f"{m}: ${b['usd']:.4f} ({b['calls']} calls, {b['in']}/{b['out']} tok)"
            for m, b in sorted(self.by_model.items(), key=lambda kv: -kv[1]["usd"])
        )
        per_tag = ", ".join(f"{t}={c}" for t, c in sorted(self.by_tag.items()))
        return (
            f"budget: ${self.used_usd:.4f} of ${self.max_usd:.2f} used "
            f"across {self.call_count} calls ({self.input_tokens} in / {self.output_tokens} out tok). "
            f"by-role: {per_tag or 'n/a'}. by-model: {per_model or 'n/a'}."
        )

    def to_report(self) -> dict:
        """Structured snapshot of the tracker for persisting to disk."""
        return {
            "max_usd": round(self.max_usd, 6),
            "used_usd": round(self.used_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "call_count": self.call_count,
            "halted": self.halted,
            "by_model": {
                m: {
                    "calls": b["calls"],
                    "input_tokens": b["in"],
                    "output_tokens": b["out"],
                    "usd": round(b["usd"], 6),
                }
                for m, b in self.by_model.items()
            },
            "by_tag": dict(self.by_tag),
        }


# --------------------------------------------------------------------------- #
# ContextVar plumbing so callers don't need to thread the tracker everywhere
# --------------------------------------------------------------------------- #


_current: contextvars.ContextVar["BudgetTracker | None"] = contextvars.ContextVar(
    "maf_generic_migrator_v1.budget_tracker", default=None
)

# Remembers the most recently finalized tracker so driver scripts can surface
# post-run cost without having to thread the tracker through return values.
_last_finalized: "BudgetTracker | None" = None


def current() -> "BudgetTracker | None":
    return _current.get()


def set_current(tracker: "BudgetTracker | None") -> None:
    global _last_finalized
    prev = _current.get()
    if tracker is None and prev is not None:
        _last_finalized = prev
    _current.set(tracker)


def last_finalized() -> "BudgetTracker | None":
    """The tracker from the most recent run, cleared or not."""
    return _last_finalized


class BudgetExceeded(RuntimeError):
    """Raised when an LLM call is attempted after the budget is exhausted."""
