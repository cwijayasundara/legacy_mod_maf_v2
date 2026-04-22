"""Tests for the Summarizer's prompt search path.

Cartridge-specific prompts must override platform defaults; kinds without
a cartridge override must fall back to platform defaults.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.platform_core.comprehension import (
    FakeChatClient,
    Summarizer,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)

PLATFORM_PROMPTS = (
    Path(__file__).resolve().parents[1]
    / "platform_core"
    / "comprehension"
    / "prompts"
)


def _toy_store() -> NetworkXStore:
    s = NetworkXStore()
    s.add_node(
        KGNode(
            id="program:X",
            kind="program",
            name="X",
            span=SourceSpan(file="x.cbl", start_line=1, end_line=1),
            raw_text="IDENTIFICATION DIVISION. PROGRAM-ID. X.",
        )
    )
    s.add_node(
        KGNode(
            id="paragraph:X:P",
            kind="paragraph",
            name="P",
            span=SourceSpan(file="x.cbl", start_line=2, end_line=2),
            raw_text="ADD 1 TO WS-A.",
        )
    )
    s.add_edge(KGEdge(source="program:X", target="paragraph:X:P", kind="contains"))
    return s


async def test_cartridge_prompt_overrides_platform_default(tmp_path: Path):
    """A cartridge-specific paragraph.md in the first dir must win over
    the platform paragraph.md in the second dir.
    """
    cartridge_dir = tmp_path / "cartridge_prompts"
    cartridge_dir.mkdir()
    (cartridge_dir / "paragraph.md").write_text(
        "Cartridge paragraph prompt — emits CARTRIDGE-MARK.\n"
    )

    client = FakeChatClient(
        canned={"CARTRIDGE-MARK": "cartridge summary"},
        default="(fallback — should not happen)",
    )
    summarizer = Summarizer(
        chat_client=client,
        prompts_dir=[cartridge_dir, PLATFORM_PROMPTS],
    )

    store = _toy_store()
    await summarizer.summarize(store)

    # The cartridge-specific prompt was used → fake client matched the marker
    # inside the system prompt, not the user message. Since FakeChatClient
    # matches on user text only, we indirectly verify by checking that the
    # system prompt content drove behaviour via the recorded call log.
    systems_seen = [sys_ for (sys_, _) in client.calls]
    assert any("CARTRIDGE-MARK" in s for s in systems_seen)


async def test_platform_fallback_when_cartridge_lacks_kind(tmp_path: Path):
    """If a cartridge overrides ``paragraph.md`` but not ``program.md``,
    the program-level summary must fall back to the platform prompt.
    """
    cartridge_dir = tmp_path / "cartridge_prompts"
    cartridge_dir.mkdir()
    (cartridge_dir / "paragraph.md").write_text("cartridge-only paragraph prompt.\n")
    # No program.md in the cartridge dir — must fall back.

    client = FakeChatClient(default="mock")
    summarizer = Summarizer(
        chat_client=client,
        prompts_dir=[cartridge_dir, PLATFORM_PROMPTS],
    )
    store = _toy_store()
    await summarizer.summarize(store)

    systems = {sys_ for (sys_, _) in client.calls}
    # The paragraph system prompt must contain cartridge text...
    assert any("cartridge-only paragraph prompt" in s for s in systems)
    # ...and the program one must NOT (it came from platform default).
    assert not any("cartridge-only paragraph prompt" in s for s in systems if "Purpose:" in s or "program summarizer" in s.lower())


async def test_single_path_still_accepted():
    """Backward compatibility: passing a single Path (not a list) works."""
    client = FakeChatClient(default="mock")
    summarizer = Summarizer(chat_client=client, prompts_dir=PLATFORM_PROMPTS)
    store = _toy_store()
    await summarizer.summarize(store)
    for node in store.iter_nodes():
        assert node.llm_summary == "mock"


async def test_empty_prompts_list_rejected():
    client = FakeChatClient()
    with pytest.raises(ValueError):
        Summarizer(chat_client=client, prompts_dir=[])


async def test_missing_everywhere_raises(tmp_path: Path):
    """If the kind isn't covered and no default.md exists anywhere in the
    chain, the summarizer must fail loudly — never silently skip a node.
    """
    empty_a = tmp_path / "a"
    empty_b = tmp_path / "b"
    empty_a.mkdir()
    empty_b.mkdir()
    client = FakeChatClient()
    summarizer = Summarizer(chat_client=client, prompts_dir=[empty_a, empty_b])
    with pytest.raises(FileNotFoundError):
        await summarizer.summarize(_toy_store())
