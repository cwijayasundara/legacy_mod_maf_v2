"""Recipe + RecipeBackend abstractions.

Each backend wraps an external deterministic engine. A recipe is referenced by
(backend_id, recipe_id, params); the backend knows how to apply it to a set of
source files and return a ``RecipeResult``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass
class Recipe:
    """A migration recipe backed by a specific engine."""

    id: str                               # 'org.openrewrite.java.migrate.UpgradeToJava22'
    backend: str                          # 'openrewrite' | 'libcst' | 'jscodeshift' | ...
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    languages: tuple[str, ...] = ()       # ('java',) — empty = all
    preconditions: list[str] = field(default_factory=list)


@dataclass
class RecipeResult:
    """The outcome of applying a recipe to a set of files."""

    recipe_id: str
    backend: str
    applied: bool                         # True if the recipe made a change
    files_changed: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    error: str | None = None


class RecipeBackend(ABC):
    """One backend = one external engine."""

    id: str                               # 'openrewrite' | 'libcst' | ...
    supported_languages: tuple[str, ...]

    @abstractmethod
    def apply(self, recipe: Recipe, source_paths: list[str], workdir: Path) -> RecipeResult:
        """Apply ``recipe`` to the given files, writing results under ``workdir``."""

    def available(self) -> bool:
        """Return True if this backend is installed and usable.

        Subclasses should override to probe for their external dependency
        (e.g. ``mvn``, ``node``, etc.).
        """
        return True


# --------------------------------------------------------------------------- #
# Application driver
# --------------------------------------------------------------------------- #


def apply_recipes(
    recipes: list[Recipe],
    source_paths: list[str],
    workdir: Path,
    backends: dict[str, RecipeBackend] | None = None,
) -> Iterator[RecipeResult]:
    """Apply recipes in order. Skips recipes whose backend isn't available.

    ``backends`` overrides the default backend lookup; if omitted, uses the
    global registry below.
    """

    backends = backends or _default_backends()
    for recipe in recipes:
        backend = backends.get(recipe.backend)
        if backend is None:
            yield RecipeResult(
                recipe_id=recipe.id,
                backend=recipe.backend,
                applied=False,
                error=f"no backend registered for '{recipe.backend}'",
            )
            continue
        if not backend.available():
            yield RecipeResult(
                recipe_id=recipe.id,
                backend=recipe.backend,
                applied=False,
                error=f"backend '{recipe.backend}' not available on this host",
            )
            continue
        try:
            yield backend.apply(recipe, source_paths, workdir)
        except Exception as exc:  # noqa: BLE001 — one bad recipe must not kill the unit
            yield RecipeResult(
                recipe_id=recipe.id,
                backend=recipe.backend,
                applied=False,
                error=f"{type(exc).__name__}: {exc}",
            )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


_BACKENDS: dict[str, RecipeBackend] = {}


def register_backend(backend: RecipeBackend) -> None:
    _BACKENDS[backend.id] = backend


def _default_backends() -> dict[str, RecipeBackend]:
    if _BACKENDS:
        return _BACKENDS
    # Lazy default registration — real backends register themselves on import
    # of ``maf_generic_migrator_v1.recipe_backends.<name>``.
    return {}
