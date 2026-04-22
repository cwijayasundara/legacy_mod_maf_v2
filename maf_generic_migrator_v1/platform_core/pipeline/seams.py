"""Seam scorer.

A "seam" (Fowler / Thoughtworks, *Uncovering Mainframe Seams*) is a junction
point where program flow can be diverted to a new service without rewriting
upstream or downstream code. This module scores candidate seams from the KG
using three signals explicitly named in that methodology:

1. **Observable interface** — the seam sits at a boundary that's easy to
   proxy, replicate, or intercept. Datasets (CDC-friendly) and CICS
   transactions (HTTP-proxyable) score high; paragraph-internal calls
   score low.
2. **Funnel / fan-in + fan-out** — how many programs converge on this
   seam. Wide funnels are leverage points — cutting one dataset moves
   many downstream consumers.
3. **Read/write asymmetry** — pure readers are the safest migration
   target (replicate the write side via CDC, rebuild the read side).
   Pure writers are second-safest. Nodes with balanced R/W are riskier.

Output is a list of ``SeamCandidate`` objects sorted by ``total_score``
descending. Evidence strings explain why each candidate ranks where it
does — human planners read these to pick the actual cut-point.

The scorer is deterministic and cartridge-agnostic. It reads only the KG
(+ a ``CRUDMatrix`` built from it) and never calls an LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from maf_generic_migrator_v1.platform_core.kg import KGStore

from .crud import CRUDMatrix, ResourceRef, build_crud_matrix


# --------------------------------------------------------------------------- #
# Weights — tunable per site but documented defaults follow the methodology
# --------------------------------------------------------------------------- #


# Observable-interface priors by resource kind. Datasets are the best seams
# because CDC (Qlik/Precisely/Kafka Connect) makes dual-run trivial. CICS
# transactions are next because they're already RPC-shaped and proxyable at
# the network layer. SQL blocks are mid-tier — you can intercept SQL via a
# proxy DB but it's more invasive. FDs and in-program calls are weakest —
# they're internal details, not observable from outside the program.
_OBSERVABLE_SCORE: dict[str, float] = {
    "dataset": 1.0,
    "txn": 0.8,
    "sql_block": 0.5,
    "file": 0.2,
    "unknown": 0.1,
}

# Component weights for the final score. Deliberately conservative on
# observable so a high-funnel internal seam still ranks, and aggressive on
# funnel so popular seams rise fast.
_WEIGHT_OBSERVABLE = 0.4
_WEIGHT_FUNNEL = 0.4
_WEIGHT_ASYMMETRY = 0.2


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SeamCandidate:
    """One ranked seam candidate."""

    resource: ResourceRef
    readers: tuple[str, ...]            # program ids that read from the resource
    writers: tuple[str, ...]            # program ids that write to it
    touches_only: tuple[str, ...]       # programs observed touching via JCL only
    observable_score: float
    funnel_score: float
    asymmetry_score: float
    total_score: float
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fanin(self) -> int:
        return len(self.readers)

    @property
    def fanout(self) -> int:
        return len(self.writers)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def rank_seams(
    store: KGStore,
    matrix: CRUDMatrix | None = None,
    *,
    min_programs: int = 1,
) -> list[SeamCandidate]:
    """Return seam candidates ordered by score descending.

    ``min_programs`` filters out resources touched by fewer than N programs
    — set to 2 to focus on actual cross-program seams and drop
    single-consumer resources that can't be "seams" by definition. Default
    is 1 so callers see every candidate.
    """
    matrix = matrix or build_crud_matrix(store)
    max_funnel = max(
        (
            len(matrix.programs_touching(r))
            for r in matrix.resources()
        ),
        default=1,
    ) or 1

    candidates: list[SeamCandidate] = []
    for resource in matrix.resources():
        programs = matrix.programs_touching(resource)
        if len(programs) < min_programs:
            continue

        readers = tuple(matrix.readers_of(resource))
        writers = tuple(matrix.writers_of(resource))
        touches_only = tuple(
            p for p in programs if p not in readers and p not in writers
        )

        observable = _OBSERVABLE_SCORE.get(resource.kind, _OBSERVABLE_SCORE["unknown"])
        funnel = len(programs) / max_funnel
        asymmetry = _asymmetry_score(len(readers), len(writers))

        total = (
            _WEIGHT_OBSERVABLE * observable
            + _WEIGHT_FUNNEL * funnel
            + _WEIGHT_ASYMMETRY * asymmetry
        )

        evidence = _render_evidence(resource, programs, readers, writers, touches_only)
        candidates.append(
            SeamCandidate(
                resource=resource,
                readers=readers,
                writers=writers,
                touches_only=touches_only,
                observable_score=round(observable, 3),
                funnel_score=round(funnel, 3),
                asymmetry_score=round(asymmetry, 3),
                total_score=round(total, 3),
                evidence=evidence,
            )
        )

    # Stable sort: highest score first; break ties by resource kind then name.
    candidates.sort(
        key=lambda c: (-c.total_score, c.resource.kind, c.resource.name)
    )
    return candidates


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _asymmetry_score(readers: int, writers: int) -> float:
    """Higher when readers vs writers are imbalanced.

    Pure readers (writers == 0) or pure writers (readers == 0) score 1.0 —
    perfect migration candidates per the methodology (replicate one side,
    rebuild the other). Equal readers and writers score 0.0 — these are
    the tangled seams.
    """
    if readers == 0 and writers == 0:
        return 0.0
    denom = max(readers, writers)
    return abs(readers - writers) / denom


def _render_evidence(
    resource: ResourceRef,
    programs: list[str],
    readers: tuple[str, ...],
    writers: tuple[str, ...],
    touches_only: tuple[str, ...],
) -> tuple[str, ...]:
    lines: list[str] = []
    lines.append(f"{resource.display} touched by {len(programs)} program(s)")
    if readers:
        lines.append(f"  readers: {', '.join(readers)}")
    if writers:
        lines.append(f"  writers: {', '.join(writers)}")
    if touches_only:
        lines.append(f"  touches (unclassified): {', '.join(touches_only)}")
    return tuple(lines)


__all__ = ["SeamCandidate", "rank_seams"]
