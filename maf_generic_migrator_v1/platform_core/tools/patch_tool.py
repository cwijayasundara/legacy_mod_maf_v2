"""Unified-diff apply helper. Minimal, stdlib-only."""
from __future__ import annotations

import difflib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def apply_unified_diff(diff_text: str, root: Path) -> list[str]:
    """Apply a unified diff to files rooted at ``root``. Returns changed file paths.

    This implementation handles the subset of diff features the translators
    produce: single-file hunks with context lines. For more complex patches,
    delegate to ``patch(1)``.
    """
    changed: list[str] = []
    lines = diff_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].startswith("--- "):
            src_hdr = lines[i]
            dst_hdr = lines[i + 1]
            target = dst_hdr[4:].strip().lstrip("b/")
            i += 2
            patch_lines: list[str] = []
            while i < len(lines) and not lines[i].startswith("--- "):
                patch_lines.append(lines[i])
                i += 1
            _apply_one_file(target, patch_lines, root)
            changed.append(target)
        else:
            i += 1
    return changed


def _apply_one_file(target: str, patch_lines: list[str], root: Path) -> None:
    target_path = root / target
    original: list[str] = []
    if target_path.exists():
        original = target_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

    # Use difflib.restore through ndiff — we feed the inverse patch structure.
    # For our minimal use we just parse hunks.
    new_lines = list(original)
    for hunk in _split_hunks(patch_lines):
        _apply_hunk(hunk, new_lines)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("".join(new_lines), encoding="utf-8")


def _split_hunks(lines: list[str]) -> list[list[str]]:
    hunks: list[list[str]] = []
    current: list[str] = []
    for ln in lines:
        if ln.startswith("@@"):
            if current:
                hunks.append(current)
            current = [ln]
        elif current:
            current.append(ln)
    if current:
        hunks.append(current)
    return hunks


def _apply_hunk(hunk: list[str], target: list[str]) -> None:
    header = hunk[0]
    # "@@ -l,s +l,s @@"
    try:
        start_old = int(header.split(" ")[1].split(",")[0].lstrip("-"))
    except ValueError:
        return
    idx = max(0, start_old - 1)
    for ln in hunk[1:]:
        if ln.startswith("-"):
            if idx < len(target) and target[idx] == ln[1:]:
                target.pop(idx)
            else:
                logger.warning("hunk mismatch at line %d: %r", idx, ln)
        elif ln.startswith("+"):
            target.insert(idx, ln[1:])
            idx += 1
        else:
            idx += 1


def make_diff(before: str, after: str, filename: str) -> str:
    """Produce a unified diff string from ``before`` -> ``after``."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )
