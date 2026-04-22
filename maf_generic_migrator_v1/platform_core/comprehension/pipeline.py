"""Comprehension pipeline orchestrator.

One call to ``run_comprehension`` takes a populated ``KGStore`` plus a
cartridge and returns a ``ComprehensionResult`` with (a) summaries written
back into the graph and (b) one ``BusinessSpec`` per program. Hides the
prompt-search-path + cache plumbing from cartridge callers.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge
from maf_generic_migrator_v1.platform_core.kg import KGStore
from maf_generic_migrator_v1.platform_core.pipeline.crud import (
    CRUDMatrix,
    build_crud_matrix,
)

from .business_spec import BusinessSpec, render_all_business_specs
from .summarizer import ChatClient, Summarizer, SummaryCache

logger = logging.getLogger(__name__)

_PLATFORM_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass
class ComprehensionResult:
    """Outputs of one comprehension run."""

    summarized_nodes: int
    crud_matrix: CRUDMatrix
    specs: list[BusinessSpec]


async def run_comprehension(
    store: KGStore,
    cartridge: MigrationCartridge,
    *,
    chat_client: ChatClient,
    cache_path: Path | None = None,
    classify: "Callable[[str], str | None] | None" = None,
    max_summaries: int | None = None,
    sample_programs: int | None = None,
) -> ComprehensionResult:
    """Run the full comprehension pipeline.

    1. Build the summarizer with cartridge → platform prompt search path.
    2. Walk the KG bottom-up, populating ``llm_summary`` on every node.
    3. Build the CRUD matrix (needed for spec's Inputs/Outputs grounding).
    4. Render one ``BusinessSpec`` per program.

    The KG is mutated in place. Callers that want an untouched copy should
    snapshot first.

    Cost controls:

    * ``max_summaries`` — hard cap on the number of fresh LLM calls
      this run makes. Enforced per node. Remaining nodes stay
      un-summarized and the business-spec renderer degrades gracefully
      (purpose shows ``(purpose not yet summarized)``). A later run
      with a bumped cap picks up via the cache.
    * ``sample_programs`` — only summarize the top-N programs by
      centrality (fan-in + fan-out of contains / calls / performs /
      step_of edges) and their descendants. Critical for running on a
      100K-LOC estate where full coverage is cost-prohibitive. When
      set, isolated non-program nodes are skipped too.
    """
    prompts_dirs = _resolve_prompts_dirs(cartridge)
    summarizer = Summarizer(
        chat_client=chat_client,
        prompts_dir=prompts_dirs,
        cache=SummaryCache(cache_path=cache_path),
    )

    root_ids: list[str] | None = None
    selected_programs: list[str] = []
    if sample_programs is not None and sample_programs > 0:
        selected_programs = _rank_programs_by_centrality(store)[:sample_programs]
        root_ids = [f"program:{pid}" for pid in selected_programs]
        logger.info(
            "comprehension sample mode: selected top %d programs by centrality: %s",
            len(selected_programs), selected_programs,
        )

    fresh = await summarizer.summarize(
        store, max_summaries=max_summaries, root_ids=root_ids,
    )
    logger.info(
        "comprehension: %d nodes summarized (prompt search: %s%s%s)",
        fresh,
        " -> ".join(str(d) for d in prompts_dirs),
        f" | cap={max_summaries}" if max_summaries is not None else "",
        f" | sample={sample_programs}" if sample_programs is not None else "",
    )

    matrix = build_crud_matrix(store)
    specs = render_all_business_specs(store, matrix=matrix, classify=classify)
    return ComprehensionResult(summarized_nodes=fresh, crud_matrix=matrix, specs=specs)


def _rank_programs_by_centrality(store: KGStore) -> list[str]:
    """Order program ids by importance for sampling.

    Centrality here = total incoming edges of kind ``calls`` (how many
    CALL sites target this program) plus total outgoing ``step_of`` and
    ``contains`` edges (richness of internal structure). Ties broken
    alphabetically. Cheap and good enough for "show me the top 20 to
    summarize first" — not a load-bearing graph-theoretic measure.
    """
    scores: list[tuple[int, str, str]] = []
    for program in store.iter_nodes(kind="program"):
        incoming_calls = sum(
            1 for _ in store.iter_edges(target=program.id, kind="calls")
        )
        outgoing_steps = sum(
            1 for _ in store.iter_edges(source=program.id, kind="step_of")
        )
        contains_kids = sum(
            1 for _ in store.iter_edges(source=program.id, kind="contains")
        )
        score = incoming_calls * 3 + outgoing_steps * 2 + contains_kids
        # Negate score so sort ascending → highest-score first. Secondary
        # key: program name for determinism.
        scores.append((-score, program.name, program.name))
    scores.sort()
    return [name for _, _, name in scores]


def _resolve_prompts_dirs(cartridge: MigrationCartridge) -> list[Path]:
    dirs: list[Path] = []
    override = cartridge.comprehension_prompts_dir()
    if override is not None and override.is_dir():
        dirs.append(override)
    dirs.append(_PLATFORM_PROMPTS_DIR)
    return dirs


__all__ = ["ComprehensionResult", "run_comprehension"]
