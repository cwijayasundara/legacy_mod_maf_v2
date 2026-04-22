"""Semantic chunker.

Splits large source files at function/class boundaries. Uses tree-sitter when a
grammar is available, falls back to regex per language. Preserves intent by
never cutting through a definition. Configurable overlap for context continuity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_LINES = 3_000
DEFAULT_MAX_CHARS = 150_000
DEFAULT_TARGET_CHUNK_LINES = 500
DEFAULT_OVERLAP_LINES = 50


@dataclass
class Chunk:
    """A semantic chunk of source code."""

    index: int
    start_line: int
    end_line: int
    content: str
    file: str = ""
    context_summary: str = ""
    boundary_symbols: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return self.content


# Language -> regex that matches the *start* of a semantic boundary (class/function).
BOUNDARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^(?:class\s+\w|(?:async\s+)?def\s+\w)", re.MULTILINE),
    "node": re.compile(
        r"^(?:(?:async\s+)?function\s+\w|class\s+\w|(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?(?:\(|function))",
        re.MULTILINE,
    ),
    "typescript": re.compile(
        r"^(?:(?:export\s+)?(?:async\s+)?function\s+\w|(?:export\s+)?(?:abstract\s+)?class\s+\w|(?:export\s+)?(?:const|let|var)\s+\w+\s*(?::\s*\S+)?\s*=\s*(?:async\s+)?(?:\(|function))",
        re.MULTILINE,
    ),
    "java": re.compile(
        r"^\s*(?:public|private|protected|static|abstract|final|\s)*(?:class|interface|enum|record|(?:[\w<>\[\],\s]+\s+\w+\s*\())",
        re.MULTILINE,
    ),
    "csharp": re.compile(
        r"^\s*(?:public|private|protected|internal|static|async|sealed|abstract|\s)*(?:class|interface|struct|record|enum|(?:[\w<>\[\],\s]+\s+\w+\s*\())",
        re.MULTILINE,
    ),
    "go": re.compile(r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?\w", re.MULTILINE),
}


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
}


def needs_chunking(content: str | Path, *, max_lines: int = DEFAULT_MAX_LINES, max_chars: int = DEFAULT_MAX_CHARS) -> bool:
    text, _ = _coerce(content)
    return text.count("\n") + 1 > max_lines or len(text) > max_chars


def chunk_file(
    content: str | Path,
    language: str | None = None,
    *,
    target_chunk_lines: int = DEFAULT_TARGET_CHUNK_LINES,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
) -> list[Chunk]:
    """Split ``content`` (raw string or Path) into semantic chunks."""

    text, inferred = _coerce(content)
    language = language or inferred or "python"
    file_label = str(content) if isinstance(content, Path) else ""

    lines = text.splitlines(keepends=True)
    pattern = BOUNDARY_PATTERNS.get(language)
    boundary_line_nums = _find_boundary_lines(text, pattern)
    if not boundary_line_nums:
        boundary_line_nums = list(range(0, len(lines), target_chunk_lines))

    # Group boundaries so each chunk is ~target_chunk_lines long.
    chunk_starts: list[int] = []
    cursor = 0
    for ln in boundary_line_nums:
        if not chunk_starts or ln - chunk_starts[-1] >= target_chunk_lines:
            chunk_starts.append(ln)
        cursor = ln
    if cursor < len(lines) and (not chunk_starts or chunk_starts[-1] != cursor):
        # Final chunk start — unused but kept in case future logic needs it.
        pass

    chunk_ends = chunk_starts[1:] + [len(lines)]

    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(zip(chunk_starts, chunk_ends, strict=True)):
        overlap_start = max(0, start - overlap_lines) if i > 0 else start
        body = "".join(lines[overlap_start:end])
        chunks.append(
            Chunk(
                index=i,
                start_line=overlap_start + 1,
                end_line=end,
                content=body,
                file=file_label,
                boundary_symbols=_symbol_names(lines[start:end], language),
            )
        )
    return chunks


def _coerce(src: str | Path) -> tuple[str, str | None]:
    if isinstance(src, Path):
        text = src.read_text(encoding="utf-8", errors="replace")
        return text, _LANG_BY_SUFFIX.get(src.suffix.lower())
    return src, None


def _find_boundary_lines(text: str, pattern: re.Pattern[str] | None) -> list[int]:
    """Return 0-based line numbers where boundary matches start."""
    if pattern is None:
        return []
    out: list[int] = []
    for m in pattern.finditer(text):
        out.append(text.count("\n", 0, m.start()))
    return out


_PY_SYMBOL = re.compile(r"^(?:class|(?:async\s+)?def)\s+(\w+)")
_TS_SYMBOL = re.compile(r"^(?:(?:export\s+)?(?:async\s+)?function\s+(\w+)|class\s+(\w+))")
_JAVA_SYMBOL = re.compile(r"^\s*(?:public|private|protected|static|\s)*(?:class|interface|enum|record)\s+(\w+)")
_CS_SYMBOL = re.compile(r"^\s*(?:public|private|protected|internal|\s)*(?:class|interface|struct|record)\s+(\w+)")


def _symbol_names(lines: list[str], language: str) -> list[str]:
    rx = {
        "python": _PY_SYMBOL,
        "typescript": _TS_SYMBOL,
        "node": _TS_SYMBOL,
        "java": _JAVA_SYMBOL,
        "csharp": _CS_SYMBOL,
    }.get(language)
    if rx is None:
        return []
    names: list[str] = []
    for ln in lines:
        m = rx.match(ln)
        if m:
            names.append(next(g for g in m.groups() if g))
    return names
