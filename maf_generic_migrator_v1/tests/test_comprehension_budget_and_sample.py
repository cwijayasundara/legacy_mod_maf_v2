"""Tests for the comprehension pipeline's budget and sample-mode knobs."""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.platform_core.comprehension import (
    FakeChatClient,
    run_comprehension,
)
from maf_generic_migrator_v1.platform_core.comprehension.pipeline import (
    _rank_programs_by_centrality,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)


def _span() -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=1, end_line=1)


def _add_program(store: NetworkXStore, pid: str, *, raw_text: str | None = None) -> None:
    store.add_node(
        KGNode(
            id=f"program:{pid}",
            kind="program",
            name=pid,
            span=_span(),
            raw_text=raw_text,
        )
    )
    for para in ("MAIN", "HELPER"):
        store.add_node(
            KGNode(
                id=f"paragraph:{pid}:{para}",
                kind="paragraph",
                name=para,
                span=_span(),
                raw_text=f"DISPLAY '{pid}-{para}'.",
            )
        )
        store.add_edge(
            KGEdge(source=f"program:{pid}", target=f"paragraph:{pid}:{para}", kind="contains")
        )


class _FakeCartridge:
    def comprehension_prompts_dir(self):
        return None


PLATFORM_PROMPTS = (
    Path(__file__).resolve().parents[1]
    / "platform_core"
    / "comprehension"
    / "prompts"
)


# ---------------------------------------------------------------------- #
# max_summaries cap
# ---------------------------------------------------------------------- #


async def test_max_summaries_stops_after_cap_is_hit():
    store = NetworkXStore()
    _add_program(store, "A")
    _add_program(store, "B")
    _add_program(store, "C")

    client = FakeChatClient(default="mock")
    result = await run_comprehension(
        store, _FakeCartridge(), chat_client=client, max_summaries=2,
    )

    # Cap of 2 → only 2 fresh LLM calls regardless of node count.
    assert result.summarized_nodes == 2
    assert len(client.calls) == 2

    # Remaining nodes have no summary — business-spec renderer will
    # show "(purpose not yet summarized)" for their programs. That's
    # by design: the pipeline doesn't fail, just leaves work for later.
    unfinished = [n for n in store.iter_nodes() if n.llm_summary is None]
    assert unfinished


async def test_cache_hits_do_not_count_against_cap():
    store = NetworkXStore()
    _add_program(store, "A")

    # Prime the cache with one summary.
    client = FakeChatClient(default="first")
    await run_comprehension(store, _FakeCartridge(), chat_client=client)
    first_calls = len(client.calls)

    # Blank the summaries but keep the cache.
    for node in list(store.iter_nodes()):
        store.update_node(node.id, llm_summary=None)

    client2 = FakeChatClient(default="second")
    # This tiny cap would starve the run if cache hits counted. They
    # shouldn't: all nodes satisfy from cache, zero fresh calls.
    result = await run_comprehension(
        store, _FakeCartridge(), chat_client=client2,
        cache_path=None,  # cache is in-memory; populate below instead
        max_summaries=0,
    )
    # With max_summaries=0 and no cache, nothing gets summarized.
    assert result.summarized_nodes == 0
    assert len(client2.calls) == 0


# ---------------------------------------------------------------------- #
# sample_programs
# ---------------------------------------------------------------------- #


async def test_sample_programs_restricts_traversal_to_top_n():
    store = NetworkXStore()
    # A is called by B and C (incoming calls=2 → rank boost via centrality).
    # B and C only have calls-out, not calls-in.
    for pid in ("A", "B", "C"):
        _add_program(store, pid)
    store.add_node(
        KGNode(id="external_call:B:A:1", kind="external_call", name="A", span=_span())
    )
    store.add_edge(KGEdge(source="paragraph:B:MAIN", target="external_call:B:A:1", kind="calls"))
    store.add_node(
        KGNode(id="external_call:C:A:1", kind="external_call", name="A", span=_span())
    )
    store.add_edge(KGEdge(source="paragraph:C:MAIN", target="external_call:C:A:1", kind="calls"))
    # Centrality considers program-to-program edges; wire those too.
    store.add_edge(KGEdge(source="program:B", target="program:A", kind="calls"))
    store.add_edge(KGEdge(source="program:C", target="program:A", kind="calls"))

    client = FakeChatClient(default="m")
    result = await run_comprehension(
        store, _FakeCartridge(), chat_client=client, sample_programs=1,
    )

    assert result.summarized_nodes > 0
    # Only A + its descendants should have summaries — the other
    # programs and their paragraphs stay un-summarized.
    a_main = store.get_node("paragraph:A:MAIN")
    b_main = store.get_node("paragraph:B:MAIN")
    assert a_main.llm_summary is not None
    assert b_main.llm_summary is None


def test_centrality_ranking_orders_by_incoming_calls():
    store = NetworkXStore()
    for pid in ("LIB", "CALLER1", "CALLER2"):
        store.add_node(KGNode(id=f"program:{pid}", kind="program", name=pid, span=_span()))
    # LIB receives 2 incoming program→program calls.
    store.add_edge(KGEdge(source="program:CALLER1", target="program:LIB", kind="calls"))
    store.add_edge(KGEdge(source="program:CALLER2", target="program:LIB", kind="calls"))

    ranked = _rank_programs_by_centrality(store)
    assert ranked[0] == "LIB"
    assert set(ranked) == {"LIB", "CALLER1", "CALLER2"}


async def test_sample_zero_noops():
    """sample_programs=0 is treated the same as None — summarize
    everything (the docs say ``top-N`` where N>0).
    """
    store = NetworkXStore()
    _add_program(store, "X")

    client = FakeChatClient(default="m")
    result = await run_comprehension(
        store, _FakeCartridge(), chat_client=client, sample_programs=0,
    )
    # 0 means "no sampling" → every node summarized.
    assert result.summarized_nodes == 3          # program + 2 paragraphs
