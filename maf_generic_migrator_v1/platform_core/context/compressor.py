"""Progressive context compressor.

Keep last N chunks at full detail, compress older chunks via extractive
summarization (function signatures, imports, comments, docstrings). No LLM
calls — this runs inside the planner.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .token_estimator import estimate_tokens


@dataclass
class CompressedContext:
    full_chunks: list[str]
    compressed_history: str
    total_original_tokens: int
    total_compressed_tokens: int


def compress_history(
    chunk_contents: list[str],
    *,
    keep_full: int = 3,
    compression_ratio: float = 0.3,
) -> CompressedContext:
    if len(chunk_contents) <= keep_full:
        n = sum(estimate_tokens(c) for c in chunk_contents)
        return CompressedContext(chunk_contents, "", n, n)

    old = chunk_contents[:-keep_full]
    recent = chunk_contents[-keep_full:]
    compressed = _extractive_compress(old, compression_ratio)

    orig = sum(estimate_tokens(c) for c in chunk_contents)
    comp = sum(estimate_tokens(c) for c in recent) + estimate_tokens(compressed)
    return CompressedContext(recent, compressed, orig, comp)


_KEY_LINE_PATTERNS = [
    re.compile(r"^\s*(?:import|from|#include|using|require|package)\s"),
    re.compile(r"^\s*(?:class|interface|enum|struct|record|def|function|async|public|private|protected)\s"),
    re.compile(r'^\s*(?:"""|\'\'\'|//|/\*|\*|#)'),
]


def _extractive_compress(chunks: list[str], ratio: float) -> str:
    parts: list[str] = []
    for i, chunk in enumerate(chunks):
        lines = chunk.splitlines()
        target = max(5, int(len(lines) * ratio))

        key_lines: list[str] = []
        for ln in lines:
            if any(p.match(ln) for p in _KEY_LINE_PATTERNS):
                key_lines.append(ln)
            if len(key_lines) >= target:
                break
        parts.append(f"--- chunk {i} (compressed) ---")
        parts.extend(key_lines)

    return "\n".join(parts)
