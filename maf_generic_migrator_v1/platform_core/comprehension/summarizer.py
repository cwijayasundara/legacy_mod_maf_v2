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

from maf_generic_migrator_v1.platform_core.context.chunker import Chunk, chunk_file
from maf_generic_migrator_v1.platform_core.context.token_estimator import (
    DEFAULT_PROFILE,
    estimate_tokens,
)
from maf_generic_migrator_v1.platform_core.kg import (
    EdgeKind,
    KGNode,
    KGStore,
    post_order,
    roots,
)

logger = logging.getLogger(__name__)

#: Default ceiling for a single LLM call's input tokens. Leaves room for
#: the system prompt + a reasonable response reservation on any 128k-class
#: model (Sonnet 4.6 = 200k, gpt-5.4 = 128k). Any node whose rendered user
#: message would exceed this falls into the chunk-and-reduce path.
DEFAULT_INPUT_TOKEN_BUDGET = 80_000

#: System prompt used for sub-node chunk summaries. Each chunk lives under
#: one node (usually a paragraph or program) and its summary becomes a
#: "virtual child" of the enclosing node's reduce prompt. Kept intentionally
#: terse so the chunk summaries remain small and composable.
_CHUNK_SYSTEM_PROMPT = (
    "You are summarizing one chunk of a larger legacy source file. The\n"
    "chunk was split at a natural language boundary (function / class /\n"
    "section). Produce ONE tight paragraph (3-5 sentences) capturing:\n"
    "\n"
    "1. The behaviour this chunk implements — concrete business or\n"
    "   technical effect, not control-flow mechanics.\n"
    "2. Any named external resources it touches (tables, buckets,\n"
    "   queues, datasets, SQL verbs, CICS commands).\n"
    "3. Any pre- or post-conditions the code enforces.\n"
    "\n"
    "Do NOT describe the surrounding file or other chunks. Do NOT\n"
    "include code. Plain prose only.\n"
)
_CHUNK_PROMPT_HASH = hashlib.sha256(
    _CHUNK_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()[:16]

_LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".cs": "csharp",
    ".go": "go",
    ".cbl": "cobol",
    ".cob": "cobol",
    ".cobol": "cobol",
}


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

    @staticmethod
    def chunk_key(node_id: str, chunk: Chunk, prompt_hash: str) -> str:
        """Cache key for a single sub-node chunk.

        Keyed on ``(node_id, chunk content hash, prompt hash)`` so that
        editing one chunk of an oversized paragraph re-summarizes only
        that chunk — the rest stay cached. Cheaper than rehashing the
        whole raw_text per chunk.
        """
        content = f"{chunk.start_line}:{chunk.end_line}:{chunk.content}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return f"{node_id}#chunk{chunk.index}:{digest}:{prompt_hash}"

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
        input_token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET,
    ) -> None:
        """``prompts_dir`` accepts a single ``Path`` or a sequence of them.

        With multiple dirs, earliest-wins lookup lets a cartridge override
        platform defaults by placing ``<kind>.md`` in a dir listed before
        the platform defaults in the sequence. Resolution still falls back
        to ``default.md`` if no ``<kind>.md`` is found in any dir.

        ``input_token_budget`` is the per-call input-token ceiling. When
        a node's rendered user message would exceed it, the summarizer
        splits the node's ``raw_text`` at semantic boundaries, summarizes
        each chunk, then reduces the chunk summaries with the node's
        kind-specific prompt. This is what lets comprehension run against
        40k-LOC files without busting model context.
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
        self._input_token_budget = input_token_budget

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

        # Oversized + has raw_text to chunk → route to chunk-and-reduce.
        # Nodes without raw_text (parents that already rely on child
        # summaries) are sent as-is — the oversize must come from
        # rolled-up child summaries, which the caller should have
        # already budgeted for.
        over_budget = estimate_tokens(user_msg) > self._input_token_budget
        if over_budget and node.raw_text:
            return await self._summarize_via_chunks(
                store, node, prompt_text, prompt_hash, cache_key,
            )
        if over_budget:
            logger.warning(
                "summarizer: node %s over budget but has no raw_text to chunk (%d tok) — sending as-is",
                node.id, estimate_tokens(user_msg),
            )

        try:
            summary = await self._client.chat(system=prompt_text, user=user_msg)
        except Exception as exc:  # noqa: BLE001 — one bad node must not kill the pass
            logger.warning("summarizer failed for node %s: %s", node.id, exc)
            return 0

        summary = summary.strip()
        store.update_node(node.id, llm_summary=summary)
        self._cache.put(cache_key, summary)
        return 1

    async def _summarize_via_chunks(
        self,
        store: KGStore,
        node: KGNode,
        prompt_text: str,
        prompt_hash: str,
        cache_key: str,
    ) -> int:
        """Map-reduce over sub-node chunks when ``node.raw_text`` is too big.

        1. Split ``raw_text`` at semantic boundaries via ``chunk_file``.
        2. Summarize each chunk (cache-keyed per chunk) with a chunk-level
           system prompt.
        3. Re-invoke the node's kind prompt with the chunk summaries in
           place of raw source — this produces the node's canonical
           ``llm_summary`` and mirrors exactly what the parent-level
           reduce step already does for non-oversize nodes.
        """
        language = self._language_of(node)
        chunks = chunk_file(node.raw_text or "", language=language)
        logger.info(
            "summarizer: chunking %s (%d chars, %d tok est) → %d chunks",
            node.id, len(node.raw_text or ""),
            estimate_tokens(node.raw_text or ""), len(chunks),
        )

        chunk_summaries: list[tuple[Chunk, str]] = []
        for chunk in chunks:
            ckey = SummaryCache.chunk_key(node.id, chunk, _CHUNK_PROMPT_HASH)
            cached = self._cache.get(ckey)
            if cached is not None:
                chunk_summaries.append((chunk, cached))
                continue
            chunk_user_msg = self._build_chunk_user_message(node, chunk, language)
            try:
                chunk_summary = await self._client.chat(
                    system=_CHUNK_SYSTEM_PROMPT, user=chunk_user_msg
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "summarizer chunk failed for %s chunk %d: %s",
                    node.id, chunk.index, exc,
                )
                chunk_summary = ""
            chunk_summary = chunk_summary.strip()
            if chunk_summary:
                self._cache.put(ckey, chunk_summary)
            chunk_summaries.append((chunk, chunk_summary))

        # Reduce: build a user message that carries chunk summaries in
        # place of raw source. Keep the existing child-summaries block so
        # any already-summarized contains-children still flow through.
        reduced_msg = self._build_reduced_user_message(
            store, node, chunk_summaries
        )
        try:
            summary = await self._client.chat(system=prompt_text, user=reduced_msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("summarizer reduce failed for %s: %s", node.id, exc)
            return 0
        summary = summary.strip()
        store.update_node(node.id, llm_summary=summary)
        self._cache.put(cache_key, summary)
        return 1

    def _build_chunk_user_message(
        self, node: KGNode, chunk: Chunk, language: str | None
    ) -> str:
        """User message for summarizing one chunk of an oversized node."""
        header = [
            "# Chunk",
            f"- parent node: `{node.id}` ({node.kind} `{node.name}`)",
            f"- chunk index: {chunk.index}",
            f"- lines: {chunk.start_line}-{chunk.end_line}",
        ]
        if chunk.boundary_symbols:
            header.append(f"- contained symbols: {', '.join(chunk.boundary_symbols)}")
        lang_tag = language or ""
        header.append(f"\n## Source\n```{lang_tag}\n{chunk.content}\n```")
        return "\n".join(header)

    def _build_reduced_user_message(
        self,
        store: KGStore,
        node: KGNode,
        chunk_summaries: list[tuple[Chunk, str]],
    ) -> str:
        """User message for the reduce step — node kind prompt + chunk summaries.

        Mirrors ``_build_user_message`` but swaps the raw source block for
        a bullet list of chunk summaries. The kind prompt (paragraph.md /
        program.md / etc.) produces a normal summary because — from its
        perspective — it's just reading children's distillations, which
        is the same shape it handles for parent nodes elsewhere.
        """
        lines: list[str] = []
        lines.append("# Node\n")
        lines.append(f"- id: `{node.id}`")
        lines.append(f"- kind: `{node.kind}`")
        lines.append(f"- name: `{node.name}`")
        if node.span is not None:
            lines.append(
                f"- span: `{node.span.file}:{node.span.start_line}-{node.span.end_line}`"
            )
        for k, v in sorted(node.attributes.items()):
            lines.append(f"- {k}: `{v}`")

        lines.append(
            "\n> This node's raw source exceeded the input-token budget\n"
            "> and was summarized via semantic chunks below. Treat these\n"
            "> chunk summaries as authoritative — they fully cover the\n"
            "> source that WOULD have been inlined here."
        )

        lines.append("\n## Chunk summaries")
        for chunk, summary in chunk_summaries:
            span = f"lines {chunk.start_line}-{chunk.end_line}"
            symbols = (
                f" ({', '.join(chunk.boundary_symbols)})"
                if chunk.boundary_symbols else ""
            )
            body = summary or "(chunk summary unavailable)"
            lines.append(f"- **chunk {chunk.index}, {span}{symbols}**: {body}")

        children = store.neighbors(
            node.id, direction="out", edge_kinds=self._traverse_edges
        )
        summarized_children = [c for c in children if c.llm_summary]
        if summarized_children:
            lines.append("\n## Child summaries")
            for child in summarized_children:
                lines.append(f"- **{child.kind} `{child.name}`**: {child.llm_summary}")
        return "\n".join(lines)

    def _language_of(self, node: KGNode) -> str | None:
        """Resolve the language for chunking.

        Priority: explicit ``attributes["language"]`` > span-file suffix >
        None (chunker will default). The polyglot adapters set language
        on the program node; paragraphs inherit via the file suffix.
        """
        lang = node.attributes.get("language")
        if lang:
            return lang
        if node.span is not None:
            from pathlib import Path as _P

            suffix = _P(node.span.file).suffix.lower()
            if suffix in _LANG_BY_SUFFIX:
                return _LANG_BY_SUFFIX[suffix]
        return None

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
