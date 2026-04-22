"""LanguageAdapter ABC.

An adapter is stateless: given a unit-root path, it returns a ``UnitIR``.
Adapters own language-specific parsing (tree-sitter queries, AST walks, regex
fallbacks). Cartridges compose adapters; the platform core only sees the IR.

Adapters may also contribute sub-unit structure to a ``KGStore`` via the
optional ``extract_kg`` hook — used by COBOL and other legacy languages where
a single program is too large to treat as one opaque unit.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from maf_generic_migrator_v1.platform_core.ir import UnitIR

if TYPE_CHECKING:
    from maf_generic_migrator_v1.platform_core.kg import KGStore


class LanguageAdapter(ABC):
    """Extract a ``UnitIR`` from a unit root path.

    ``unit_root`` may be either a directory (multi-file unit) or a single
    source file (per-handler unit, used when one source dir contains many
    independent Lambda handlers — e.g. ``handlers/{a,b,c,d}.py``).
    """

    language: str          # 'python' | 'node' | 'typescript' | 'java' | 'csharp' | ...
    supported_suffixes: tuple[str, ...]

    @abstractmethod
    def extract_unit(self, repo_root: Path, unit_root: Path) -> UnitIR:
        """Parse the unit (file or directory) and return a UnitIR."""

    def files_in_unit(self, unit_root: Path) -> list[Path]:
        if unit_root.is_file():
            return [unit_root]
        out: list[Path] = []
        for suffix in self.supported_suffixes:
            out.extend(sorted(unit_root.rglob(f"*{suffix}")))
        return out

    def extract_kg(self, repo_root: Path, unit_root: Path, store: "KGStore") -> None:
        """Optional hook to populate ``store`` with sub-unit nodes and edges.

        Default is a no-op — adapters that only need flat ``UnitIR`` (Python,
        Node, Java-as-Lambda) don't have to implement this. Legacy adapters
        (COBOL, PL/I) override it to add Program/Section/Paragraph/File nodes
        and their relations. Called by the cartridge during discovery.
        """
        return None

    def unit_id_for(self, repo_root: Path, unit_root: Path) -> str:
        try:
            rel = unit_root.relative_to(repo_root)
        except ValueError:
            return unit_root.stem if unit_root.is_file() else unit_root.name
        # For file units, drop the suffix so the id is e.g. "handlers.data_generator"
        # rather than "handlers.data_generator.py".
        if unit_root.is_file():
            rel = rel.with_suffix("")
        return str(rel).replace("/", ".").replace("\\", ".").strip(".") or unit_root.stem
