"""Auto-chunking in the bottom-up summarizer.

Protects the 40k-LOC-per-file scenario: when a single node's ``raw_text``
would bust the input-token budget, the summarizer must NOT fail — it
must split at semantic boundaries, summarize each chunk, then reduce the
chunk summaries with the node's kind prompt.

Tests use an intentionally tiny ``input_token_budget`` so the trigger
fires on fixture-sized code rather than requiring megabytes of text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.platform_core.comprehension.summarizer import (
    FakeChatClient,
    Summarizer,
    SummaryCache,
    _CHUNK_PROMPT_HASH,
)
from maf_generic_migrator_v1.platform_core.context.chunker import Chunk
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)

PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "platform_core"
    / "comprehension"
    / "prompts"
)


def _paragraph_with_n_functions(n: int) -> str:
    """Build a paragraph raw_text with ``n`` top-level def statements.

    Each def sits on a natural chunker boundary (``^def\\s+\\w``), so the
    semantic chunker has real cut points to split on rather than falling
    back to blind line slicing.
    """
    chunks = []
    for i in range(n):
        chunks.append(
            f"def helper_{i}(x):\n"
            f"    # helper {i} does some work\n"
            f"    y = x * {i}\n"
            f"    return y\n"
        )
    return "\n".join(chunks)


@pytest.fixture()
def oversized_store() -> NetworkXStore:
    """One program + one paragraph whose raw_text exceeds a tiny budget."""
    s = NetworkXStore()
    s.add_node(
        KGNode(
            id="program:big_lambda",
            kind="program",
            name="big_lambda",
            attributes={"language": "python"},
            span=SourceSpan(file="big_lambda.py", start_line=1, end_line=800),
        )
    )
    # ~200 helper functions so the paragraph is unambiguously oversized
    # under the 500-token test budget while staying fast to parse.
    raw = _paragraph_with_n_functions(200)
    s.add_node(
        KGNode(
            id="paragraph:big_lambda:handler",
            kind="paragraph",
            name="handler",
            span=SourceSpan(file="big_lambda.py", start_line=1, end_line=800),
            raw_text=raw,
            attributes={"language": "python"},
        )
    )
    s.add_edge(
        KGEdge(
            source="program:big_lambda",
            target="paragraph:big_lambda:handler",
            kind="contains",
        )
    )
    return s


async def test_oversized_node_chunks_and_reduces(oversized_store: NetworkXStore):
    client = FakeChatClient(default="chunked paragraph summary")
    summarizer = Summarizer(
        chat_client=client,
        prompts_dir=PROMPTS_DIR,
        input_token_budget=500,  # deliberately tiny so the paragraph busts
    )
    fresh = await summarizer.summarize(oversized_store)
    assert fresh >= 2  # at least 1 chunk summary + 1 paragraph reduce

    para = oversized_store.get_node("paragraph:big_lambda:handler")
    assert para.llm_summary == "chunked paragraph summary"

    # Multiple chat calls must have landed — one per chunk plus the
    # reduce plus the program node summary. Having more than one chunk
    # call is how we know chunking ran.
    chunk_system_calls = [
        (sys, usr) for (sys, usr) in client.calls
        if "You are summarizing one chunk" in sys
    ]
    assert len(chunk_system_calls) >= 2, (
        f"expected >=2 chunk-system calls, got {len(chunk_system_calls)}"
    )
    # Each chunk call's user message must carry the paragraph's parent id.
    for _, user_msg in chunk_system_calls:
        assert "paragraph:big_lambda:handler" in user_msg

    # The reduce call (kind prompt + chunk summaries, no raw source) must
    # explicitly mention chunk summaries instead of the source block.
    reduce_calls = [
        usr for (sys, usr) in client.calls
        if "paragraph-level" in sys.lower() and "Chunk summaries" in usr
    ]
    assert reduce_calls, "expected a reduce call with Chunk summaries section"
    assert "## Source" not in reduce_calls[0], (
        "reduce prompt must not carry raw source — that's what busted the budget"
    )


async def test_chunk_cache_hit_skips_llm_call(
    tmp_path: Path, oversized_store: NetworkXStore
):
    """Second run with a persistent cache re-uses every chunk summary."""
    cache_path = tmp_path / "cache.jsonl"

    client1 = FakeChatClient(default="cached-run-1")
    summarizer1 = Summarizer(
        chat_client=client1,
        prompts_dir=PROMPTS_DIR,
        cache=SummaryCache(cache_path),
        input_token_budget=500,
    )
    await summarizer1.summarize(oversized_store)
    first_run_calls = len(client1.calls)
    assert first_run_calls > 0

    # Fresh store, same cache — no new chat calls should fire for the
    # chunked paragraph because its chunks are already in the cache.
    store2 = NetworkXStore()
    for node in oversized_store.iter_nodes():
        # Drop any llm_summary so the summarizer re-evaluates from scratch.
        node.llm_summary = None
        store2.add_node(node)
    for edge in oversized_store.iter_edges():
        store2.add_edge(edge)

    client2 = FakeChatClient(default="cached-run-2")
    summarizer2 = Summarizer(
        chat_client=client2,
        prompts_dir=PROMPTS_DIR,
        cache=SummaryCache(cache_path),
        input_token_budget=500,
    )
    await summarizer2.summarize(store2)
    # Every chunk is cache-hit AND the paragraph's reduce key is cached
    # (same cache_key, unchanged raw_text + prompt). Net LLM calls for
    # the second run should be strictly less than the first.
    assert len(client2.calls) < first_run_calls, (
        f"expected second run to skip cached chunks; "
        f"calls: run1={first_run_calls}, run2={len(client2.calls)}"
    )


def test_chunk_cache_key_is_stable_per_chunk():
    """``SummaryCache.chunk_key`` is deterministic + chunk-specific."""
    chunk_a = Chunk(index=0, start_line=1, end_line=50, content="def a(): pass\n")
    chunk_b = Chunk(index=1, start_line=51, end_line=100, content="def b(): pass\n")

    k_a_1 = SummaryCache.chunk_key("paragraph:foo:bar", chunk_a, _CHUNK_PROMPT_HASH)
    k_a_2 = SummaryCache.chunk_key("paragraph:foo:bar", chunk_a, _CHUNK_PROMPT_HASH)
    k_b = SummaryCache.chunk_key("paragraph:foo:bar", chunk_b, _CHUNK_PROMPT_HASH)
    assert k_a_1 == k_a_2
    assert k_a_1 != k_b
    # Editing chunk content flips the key — the key is how we catch
    # stale cache entries on the second run after a source edit.
    chunk_a_edited = Chunk(
        index=0, start_line=1, end_line=50, content="def a(): return 1\n"
    )
    k_a_edited = SummaryCache.chunk_key(
        "paragraph:foo:bar", chunk_a_edited, _CHUNK_PROMPT_HASH
    )
    assert k_a_edited != k_a_1


async def test_small_node_takes_fast_path(oversized_store: NetworkXStore):
    """Nodes under budget must NOT trigger chunking — verifies the trigger."""
    small_store = NetworkXStore()
    small_store.add_node(
        KGNode(
            id="program:small",
            kind="program",
            name="small",
            raw_text="def f(): return 1\n",
            attributes={"language": "python"},
            span=SourceSpan(file="small.py", start_line=1, end_line=2),
        )
    )
    client = FakeChatClient(default="ok")
    summarizer = Summarizer(
        chat_client=client,
        prompts_dir=PROMPTS_DIR,
        input_token_budget=5000,  # small node is well under 5k tokens
    )
    await summarizer.summarize(small_store)

    # No chunk-system call should exist — fast path only.
    chunk_calls = [
        sys for (sys, _) in client.calls
        if "You are summarizing one chunk" in sys
    ]
    assert chunk_calls == []
