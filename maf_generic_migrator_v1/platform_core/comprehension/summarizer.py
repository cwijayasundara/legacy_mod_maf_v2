"""Bottom-up comprehension summarizer.

Traverses the KG in DFS post-order and calls an LLM at each node to produce a
compact ``llm_summary``. Children's summaries feed the parent's prompt, so by
the time a program-level node is summarized the prompt carries tight,
already-distilled summaries of every paragraph below it — not raw COBOL.

This is the layer that makes large legacy modules tractable: a 15K-LOC program
never hits the translator's context window in raw form, only as a spec built
from summarized pieces.

LLM access is abstracted behind ``ChatClient`` so this module has no MAF
dependency. Tests use ``FakeChatClient``; production will wrap whichever chat
client the runtime already builds (``agent_factory.build_chat_client``).
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from maf_generic_migrator_v1.platform_core.kg import (
    EdgeKind,
    KGNode,
    KGStore,
    post_order,
    roots,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Chat client abstraction (LLM-agnostic, MAF-free)
# --------------------------------------------------------------------------- #


class ChatClient(Protocol):
    """Minimal async chat interface the summarizer depends on."""

    async def chat(self, *, system: str, user: str) -> str:
        ...


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


class SummaryCache:
    """File-backed cache keyed by (node_id, content_hash, prompt_hash).

    Keeps re-runs cheap: only nodes whose source or prompt changed get
    re-summarized. The cache is a simple JSONL append log; a dict index is
    rebuilt in memory on load.
    """

    def __init__(self, cache_path: Path | None) -> None:
        self.cache_path = cache_path
        self._entries: dict[str, str] = {}
        if cache_path is not None and cache_path.is_file():
            for line in cache_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    self._entries[rec["key"]] = rec["summary"]
                except (json.JSONDecodeError, KeyError):
                    continue

    @staticmethod
    def key(node: KGNode, prompt_hash: str) -> str:
        content = (node.raw_text or "") + "|" + node.name + "|" + node.kind
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return f"{node.id}:{digest}:{prompt_hash}"

    def get(self, key: str) -> str | None:
        return self._entries.get(key)

    def put(self, key: str, summary: str) -> None:
        self._entries[key] = summary
        if self.cache_path is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "summary": summary}) + "\n")


# --------------------------------------------------------------------------- #
# Summarizer
# --------------------------------------------------------------------------- #


class Summarizer:
    """Post-order bottom-up summarizer over a ``KGStore``.

    Usage::

        s = Summarizer(chat_client=client, prompts_dir=Path("prompts"))
        await s.summarize(store)
    """

    def __init__(
        self,
        *,
        chat_client: ChatClient,
        prompts_dir: Path | Sequence[Path],
        cache: SummaryCache | None = None,
        traverse_edges: tuple[EdgeKind, ...] = ("contains",),
    ) -> None:
        """``prompts_dir`` accepts a single ``Path`` or a sequence of them.

        With multiple dirs, earliest-wins lookup lets a cartridge override
        platform defaults by placing ``<kind>.md`` in a dir listed before
        the platform defaults in the sequence. Resolution still falls back
        to ``default.md`` if no ``<kind>.md`` is found in any dir.
        """
        self._client = chat_client
        if isinstance(prompts_dir, Path):
            self._prompts_dirs: list[Path] = [prompts_dir]
        else:
            self._prompts_dirs = list(prompts_dir)
        if not self._prompts_dirs:
            raise ValueError("prompts_dir must be a Path or a non-empty sequence of Paths")
        self._cache = cache or SummaryCache(cache_path=None)
        self._traverse_edges = traverse_edges
        self._prompt_cache: dict[str, tuple[str, str]] = {}  # kind -> (text, hash)

    async def summarize(
        self,
        store: KGStore,
        *,
        max_summaries: int | None = None,
        root_ids: list[str] | None = None,
    ) -> int:
        """Summarize every node reachable from any contains-root.

        ``max_summaries`` caps the number of fresh LLM calls this pass
        will make. Cached hits don't count against the cap. Once hit,
        the pass exits early, leaving remaining nodes un-summarized —
        the pipeline continues without error, and a later invocation
        can pick up where this one left off (cache makes it cheap).

        ``root_ids`` restricts traversal to the specified roots (and
        everything below them via contains edges). Used by the sample
        mode in ``run_comprehension``.

        Returns the number of nodes that received a fresh (non-cached)
        summary. Nodes whose summary is already set are skipped.
        """
        fresh = 0
        if root_ids is None:
            traversal_roots = [r.id for r in roots(store, edge_kind="contains")]
        else:
            traversal_roots = list(root_ids)

        for root_id in traversal_roots:
            if max_summaries is not None and fresh >= max_summaries:
                logger.info("summarizer: hit max_summaries=%d, stopping", max_summaries)
                break
            fresh += await self._summarize_from(store, root_id, max_summaries, fresh)

        # Hit isolated / non-descendant nodes only when not in sample mode.
        if root_ids is None:
            for node in store.iter_nodes():
                if node.llm_summary is not None:
                    continue
                if max_summaries is not None and fresh >= max_summaries:
                    break
                fresh += await self._summarize_one(store, node)
        return fresh

    async def _summarize_from(
        self,
        store: KGStore,
        root_id: str,
        max_summaries: int | None = None,
        already_fresh: int = 0,
    ) -> int:
        fresh = 0
        for node in post_order(store, root_id, edge_kinds=self._traverse_edges):
            if max_summaries is not None and already_fresh + fresh >= max_summaries:
                break
            if node.llm_summary is not None:
                continue
            fresh += await self._summarize_one(store, node)
        return fresh

    async def _summarize_one(self, store: KGStore, node: KGNode) -> int:
        prompt_text, prompt_hash = self._load_prompt(node.kind)
        cache_key = SummaryCache.key(node, prompt_hash)
        cached = self._cache.get(cache_key)
        if cached is not None:
            store.update_node(node.id, llm_summary=cached)
            return 0

        user_msg = self._build_user_message(store, node)
        try:
            summary = await self._client.chat(system=prompt_text, user=user_msg)
        except Exception as exc:  # noqa: BLE001 — one bad node must not kill the pass
            logger.warning("summarizer failed for node %s: %s", node.id, exc)
            return 0

        summary = summary.strip()
        store.update_node(node.id, llm_summary=summary)
        self._cache.put(cache_key, summary)
        return 1

    # -- Prompt loading --------------------------------------------------- #

    def _load_prompt(self, kind: str) -> tuple[str, str]:
        if kind in self._prompt_cache:
            return self._prompt_cache[kind]

        # Specific wins over generic, earliest dir wins over later. A cartridge
        # dropping its ``program.md`` into position [0] overrides the platform
        # default at position [1]; a cartridge without a specific
        # ``field.md`` falls back to platform's ``default.md``.
        for filename in (f"{kind}.md", "default.md"):
            for prompts_dir in self._prompts_dirs:
                path = prompts_dir / filename
                if path.is_file():
                    text = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                    self._prompt_cache[kind] = (text, digest)
                    return text, digest

        search_display = " -> ".join(str(d) for d in self._prompts_dirs)
        raise FileNotFoundError(
            f"no comprehension prompt for kind={kind!r} in search path {search_display}"
        )

    # -- Message construction --------------------------------------------- #

    def _build_user_message(self, store: KGStore, node: KGNode) -> str:
        """Render the user message for one node.

        Contains: the node's own raw text (when present) and already-computed
        summaries of its direct contains-children. We do NOT recursively inline
        grandchild summaries — the parent summary should rely on its immediate
        children's distillations, not on the full subtree.
        """
        lines: list[str] = []
        lines.append(f"# Node\n")
        lines.append(f"- id: `{node.id}`")
        lines.append(f"- kind: `{node.kind}`")
        lines.append(f"- name: `{node.name}`")
        if node.span is not None:
            lines.append(f"- span: `{node.span.file}:{node.span.start_line}-{node.span.end_line}`")
        for k, v in sorted(node.attributes.items()):
            lines.append(f"- {k}: `{v}`")
        if node.raw_text:
            lines.append("\n## Source\n```\n" + node.raw_text + "\n```")

        children = store.neighbors(
            node.id, direction="out", edge_kinds=self._traverse_edges
        )
        summarized_children = [c for c in children if c.llm_summary]
        if summarized_children:
            lines.append("\n## Child summaries")
            for child in summarized_children:
                lines.append(f"- **{child.kind} `{child.name}`**: {child.llm_summary}")

        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Test helper
# --------------------------------------------------------------------------- #


class FakeChatClient:
    """Trivial ``ChatClient`` for tests — records calls, returns canned replies.

    Not exported from the package ``__init__`` because it's a test fixture,
    but importable from ``...comprehension.summarizer`` for convenience.
    """

    def __init__(self, canned: dict[str, str] | None = None, default: str = "(mock summary)") -> None:
        self._canned = canned or {}
        self._default = default
        self.calls: list[tuple[str, str]] = []

    async def chat(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        # Longest key first so a specific child summary like
        # "adds overtime hours" wins over a generic id fragment like "ADD-OT"
        # when the parent's prompt contains both.
        for key in sorted(self._canned, key=len, reverse=True):
            if key in user:
                return self._canned[key]
        return self._default
