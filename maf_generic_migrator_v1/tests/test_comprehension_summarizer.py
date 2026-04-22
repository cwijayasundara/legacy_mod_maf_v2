"""Tests for the bottom-up comprehension summarizer."""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.platform_core.comprehension import (
    FakeChatClient,
    SummaryCache,
    Summarizer,
)
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


def _node(nid: str, kind: str, raw: str | None = None) -> KGNode:
    return KGNode(
        id=nid,
        kind=kind,
        name=nid,
        span=SourceSpan(file="PAYROLL.cbl", start_line=1, end_line=10),
        raw_text=raw,
    )


@pytest.fixture()
def toy_store() -> NetworkXStore:
    """Three-level KG for the summarizer to walk."""
    s = NetworkXStore()
    s.add_node(_node("PAYROLL", "program", raw="IDENTIFICATION DIVISION. PROGRAM-ID. PAYROLL."))
    s.add_node(_node("COMPUTE-GROSS", "section", raw="COMPUTE-GROSS SECTION."))
    s.add_node(_node("ADD-OT", "paragraph", raw="ADD WS-OT TO WS-GROSS."))
    s.add_node(_node("WRITE-OUTPUT", "section", raw="WRITE-OUTPUT SECTION."))
    s.add_node(_node("EMIT-ROW", "paragraph", raw="WRITE PAYROLL-REC."))
    for src, dst in [
        ("PAYROLL", "COMPUTE-GROSS"),
        ("PAYROLL", "WRITE-OUTPUT"),
        ("COMPUTE-GROSS", "ADD-OT"),
        ("WRITE-OUTPUT", "EMIT-ROW"),
    ]:
        s.add_edge(KGEdge(source=src, target=dst, kind="contains"))
    return s


async def test_summarizer_populates_every_node(toy_store: NetworkXStore):
    client = FakeChatClient(default="mock summary")
    summarizer = Summarizer(chat_client=client, prompts_dir=PROMPTS_DIR)

    fresh = await summarizer.summarize(toy_store)

    assert fresh == 5
    for node in toy_store.iter_nodes():
        assert node.llm_summary == "mock summary", f"{node.id} not summarized"


async def test_summarizer_runs_bottom_up(toy_store: NetworkXStore):
    """Parent prompts must reference already-populated child summaries.

    We detect this by having the FakeChatClient return different canned
    replies depending on what's in the user message: the program-level
    call must include the section-level summaries.
    """
    client = FakeChatClient(
        canned={
            "ADD-OT": "adds overtime hours",
            "EMIT-ROW": "writes one payroll row",
            # Parents must see child summaries in their prompt.
            "adds overtime hours": "computes gross pay",
            "writes one payroll row": "writes the output",
            "computes gross pay": "payroll program summary",
        }
    )
    summarizer = Summarizer(chat_client=client, prompts_dir=PROMPTS_DIR)
    await summarizer.summarize(toy_store)

    assert toy_store.get_node("ADD-OT").llm_summary == "adds overtime hours"
    assert toy_store.get_node("COMPUTE-GROSS").llm_summary == "computes gross pay"
    assert toy_store.get_node("PAYROLL").llm_summary == "payroll program summary"


async def test_summarizer_skips_already_summarized(toy_store: NetworkXStore):
    toy_store.update_node("ADD-OT", llm_summary="PRESET")
    client = FakeChatClient(default="fresh")
    summarizer = Summarizer(chat_client=client, prompts_dir=PROMPTS_DIR)

    fresh = await summarizer.summarize(toy_store)

    # Only 4 of the 5 were summarized this run.
    assert fresh == 4
    assert toy_store.get_node("ADD-OT").llm_summary == "PRESET"


async def test_cache_prevents_second_llm_call(toy_store: NetworkXStore, tmp_path: Path):
    cache_path = tmp_path / "summaries.jsonl"
    cache = SummaryCache(cache_path=cache_path)

    client1 = FakeChatClient(default="from-llm")
    summarizer1 = Summarizer(chat_client=client1, prompts_dir=PROMPTS_DIR, cache=cache)
    await summarizer1.summarize(toy_store)
    first_call_count = len(client1.calls)
    assert first_call_count == 5
    assert cache_path.is_file()

    # Simulate a re-run from a blank graph but the same cache.
    rebuilt = NetworkXStore()
    rebuilt.load(toy_store.snapshot())
    for node in rebuilt.iter_nodes():
        rebuilt.update_node(node.id, llm_summary=None)

    client2 = FakeChatClient(default="should-not-be-used")
    cache2 = SummaryCache(cache_path=cache_path)  # re-load from disk
    summarizer2 = Summarizer(chat_client=client2, prompts_dir=PROMPTS_DIR, cache=cache2)
    fresh = await summarizer2.summarize(rebuilt)

    # Cache hits: zero fresh LLM calls, but every node got a summary.
    assert fresh == 0
    assert len(client2.calls) == 0
    for node in rebuilt.iter_nodes():
        assert node.llm_summary == "from-llm"


async def test_summarizer_falls_back_to_default_prompt(toy_store: NetworkXStore):
    """A node of a kind without a specific prompt must fall back to default.md."""
    toy_store.add_node(_node("FD-ACCOUNTS", "file", raw="FD ACCOUNTS LABEL RECORDS STANDARD."))
    toy_store.add_edge(KGEdge(source="PAYROLL", target="FD-ACCOUNTS", kind="contains"))

    client = FakeChatClient(default="file summary")
    summarizer = Summarizer(chat_client=client, prompts_dir=PROMPTS_DIR)
    await summarizer.summarize(toy_store)

    assert toy_store.get_node("FD-ACCOUNTS").llm_summary == "file summary"


async def test_missing_prompt_raises(tmp_path: Path, toy_store: NetworkXStore):
    empty = tmp_path / "empty_prompts"
    empty.mkdir()
    client = FakeChatClient()
    summarizer = Summarizer(chat_client=client, prompts_dir=empty)

    with pytest.raises(FileNotFoundError):
        await summarizer.summarize(toy_store)
