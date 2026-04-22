"""Discovery driver.

Walks a repo, delegates per-unit scanning to the cartridge's adapters, and
produces an ``Inventory``. Cartridge-agnostic.
"""
from __future__ import annotations

import logging
from pathlib import Path

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge
from maf_generic_migrator_v1.platform_core.ir import Inventory, UnitIR

logger = logging.getLogger(__name__)


def discover(repo_root: Path, cartridge: MigrationCartridge) -> Inventory:
    """Scan ``repo_root`` according to ``cartridge`` and return the Inventory."""

    repo_root = repo_root.resolve()
    unit_roots = cartridge.unit_classifier(repo_root)
    adapters = cartridge.adapters()

    units: list[UnitIR] = []
    warnings: list[str] = []

    for unit_root in unit_roots:
        try:
            language = _detect_language(unit_root, adapters.keys())
        except ValueError as exc:
            warnings.append(f"[skip] {unit_root.relative_to(repo_root)}: {exc}")
            continue

        adapter = adapters.get(language)
        if adapter is None:
            warnings.append(f"[skip] {unit_root.relative_to(repo_root)}: no adapter for {language}")
            continue

        try:
            unit = adapter.extract_unit(repo_root=repo_root, unit_root=unit_root)
            units.append(unit)
        except Exception as exc:  # noqa: BLE001 — intentional: one bad unit must not fail discovery
            warnings.append(f"[error] {unit_root.relative_to(repo_root)}: {exc!r}")
            logger.warning("adapter failed for %s", unit_root, exc_info=True)

    return Inventory(
        repo_root=str(repo_root),
        cartridge_id=cartridge.id,
        units=units,
        scan_warnings=warnings,
    )


_LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".cs": "csharp",
    ".cbl": "cobol",
    ".cob": "cobol",
    ".go": "go",
    ".rb": "ruby",
}


def _detect_language(unit_root: Path, supported: list[str] | set | dict) -> str:
    """Heuristic language detection.

    ``unit_root`` may be a directory (count files by extension) or a file
    (use the file's own extension).
    """

    supported = set(supported)

    if unit_root.is_file():
        lang = _LANG_BY_SUFFIX.get(unit_root.suffix.lower())
        if lang and lang in supported:
            return lang
        raise ValueError(f"no adapter for {unit_root.suffix} ({unit_root.name})")

    counts: dict[str, int] = {}
    for path in unit_root.rglob("*"):
        if not path.is_file():
            continue
        lang = _LANG_BY_SUFFIX.get(path.suffix.lower())
        if lang and lang in supported:
            counts[lang] = counts.get(lang, 0) + 1

    if not counts:
        raise ValueError(f"no recognized source files under {unit_root}")

    return max(counts.items(), key=lambda kv: kv[1])[0]
