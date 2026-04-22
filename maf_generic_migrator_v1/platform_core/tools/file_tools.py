"""File-level tools exposed to agents.

Agents receive curated context from the platform first; these tools are the
*fallback* path when a subagent needs to discover something outside its
``context_paths``. Every call is logged so the platform can compute the
"discovery gap" metric (how often curated context is insufficient).
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def read_file(path: str, offset: int = 0, limit: int | None = None) -> str:
    """Read ``path``. Optionally slice by line range.

    Logged for discovery-gap telemetry; agents should prefer using ``source_paths``
    from the backlog item before reaching for this.
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if offset:
        lines = lines[offset:]
    if limit is not None:
        lines = lines[:limit]
    logger.debug("read_file: %s offset=%d limit=%s bytes=%d", path, offset, limit, sum(len(l) for l in lines))
    return "".join(lines)


def write_file(path: str, content: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    logger.debug("write_file: %s bytes=%d", path, len(content))


def glob_files(pattern: str, root: str = ".") -> list[str]:
    root_path = Path(root)
    out: list[str] = []
    for p in root_path.rglob("*"):
        if p.is_file() and fnmatch.fnmatch(str(p.relative_to(root_path)), pattern):
            out.append(str(p))
    logger.debug("glob: pattern=%s root=%s matches=%d", pattern, root, len(out))
    return out


def grep(pattern: str, root: str = ".", *, ignore_case: bool = False, include: str | None = None) -> list[dict]:
    """Find ``pattern`` across ``root``. Returns [{path, line, match}, ...]."""
    flags = re.IGNORECASE if ignore_case else 0
    rx = re.compile(pattern, flags)
    results: list[dict] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if include and not fnmatch.fnmatch(name, include):
                continue
            p = Path(dirpath) / name
            try:
                for ln_no, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                    if rx.search(ln):
                        results.append({"path": str(p), "line": ln_no, "match": ln.strip()[:200]})
            except OSError:
                continue
    logger.debug("grep: pattern=%s root=%s hits=%d", pattern, root, len(results))
    return results
