"""LibCST recipe backend for Python AST rewrites.

Recipes are dotted paths to ``libcst.CSTTransformer`` subclasses. The backend
loads the class, applies it to each source file, and writes changes back to
``workdir / <rel_path>``.

Example recipe::

    Recipe(
        id="my_cartridge.recipes.PrintToFunction",
        backend="libcst",
        languages=("python",),
    )
"""
from __future__ import annotations

import importlib
import shutil
from pathlib import Path

from .base import Recipe, RecipeBackend, RecipeResult, register_backend


class LibCSTBackend(RecipeBackend):
    id = "libcst"
    supported_languages = ("python",)

    def available(self) -> bool:
        try:
            importlib.import_module("libcst")
            return True
        except ImportError:
            return False

    def apply(self, recipe: Recipe, source_paths: list[str], workdir: Path) -> RecipeResult:
        import libcst as cst  # type: ignore[import-not-found]

        transformer_cls = _resolve_dotted_path(recipe.id)
        if transformer_cls is None:
            return RecipeResult(
                recipe_id=recipe.id,
                backend=self.id,
                applied=False,
                error=f"cannot resolve transformer class {recipe.id}",
            )

        files_changed: list[str] = []
        for src in source_paths:
            src_path = Path(src)
            if not src_path.is_file():
                continue
            text = src_path.read_text(encoding="utf-8", errors="replace")
            try:
                module = cst.parse_module(text)
            except cst.ParserSyntaxError:
                continue
            modified = module.visit(transformer_cls(**recipe.params))
            new_text = modified.code
            if new_text != text:
                out = workdir / src_path.name
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(new_text, encoding="utf-8")
                files_changed.append(str(out))
            else:
                # Copy unchanged so downstream stages see a complete tree.
                out = workdir / src_path.name
                if not out.exists():
                    shutil.copy2(src_path, out)

        return RecipeResult(
            recipe_id=recipe.id,
            backend=self.id,
            applied=bool(files_changed),
            files_changed=files_changed,
        )


#: Packages tried as a prefix when a bare dotted-path import fails.
#: Cartridges commonly use the short form (``cartridges.X.recipes.Y``)
#: relative to the top-level package — the resolver promotes those
#: automatically so recipes don't have to hard-code the top-level name.
_TOP_LEVEL_PACKAGE_CANDIDATES = ("maf_generic_migrator_v1",)


def _resolve_dotted_path(path: str):
    """Resolve ``path`` to an attribute (typically a class).

    Tries in order:

    1. The path as given.
    2. The path with each candidate top-level package prepended.

    This lets recipe IDs use the cartridge-relative short form
    (``cartridges.X.recipes.Y``) while the real module lives under
    ``maf_generic_migrator_v1.cartridges.X.recipes``.
    """
    candidates: list[str] = [path]
    for pkg in _TOP_LEVEL_PACKAGE_CANDIDATES:
        if not path.startswith(pkg + "."):
            candidates.append(f"{pkg}.{path}")

    for candidate in candidates:
        mod_path, _, attr = candidate.rpartition(".")
        if not mod_path:
            continue
        try:
            module = importlib.import_module(mod_path)
        except ImportError:
            continue
        resolved = getattr(module, attr, None)
        if resolved is not None:
            return resolved
    return None


register_backend(LibCSTBackend())
