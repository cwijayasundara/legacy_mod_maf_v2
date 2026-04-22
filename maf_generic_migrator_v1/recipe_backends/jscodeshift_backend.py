"""jscodeshift bridge for JS/TS codemods.

Recipes reference transform files (``codemod.js``) relative to the recipe pack.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import Recipe, RecipeBackend, RecipeResult, register_backend


class JSCodeshiftBackend(RecipeBackend):
    id = "jscodeshift"
    supported_languages = ("node", "typescript")

    def available(self) -> bool:
        return shutil.which("jscodeshift") is not None or shutil.which("npx") is not None

    def apply(self, recipe: Recipe, source_paths: list[str], workdir: Path) -> RecipeResult:
        transform = recipe.params.get("transform")
        if not transform:
            return RecipeResult(
                recipe_id=recipe.id,
                backend=self.id,
                applied=False,
                error="recipe.params['transform'] is required (path to codemod)",
            )

        cmd = self._resolve_cmd() + ["-t", transform, *source_paths]
        if recipe.params.get("parser"):
            cmd += ["--parser", recipe.params["parser"]]

        try:
            proc = subprocess.run(cmd, cwd=workdir, check=False, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return RecipeResult(
                recipe_id=recipe.id,
                backend=self.id,
                applied=False,
                error="jscodeshift timed out after 300s",
            )

        changed = any("ok " in line or " modified" in line for line in proc.stdout.splitlines())
        return RecipeResult(
            recipe_id=recipe.id,
            backend=self.id,
            applied=changed,
            diagnostics=proc.stdout.splitlines()[-10:],
            error=None if proc.returncode == 0 else f"exit={proc.returncode}: {proc.stderr[-400:]}",
        )

    @staticmethod
    def _resolve_cmd() -> list[str]:
        if shutil.which("jscodeshift"):
            return ["jscodeshift"]
        return ["npx", "jscodeshift"]


register_backend(JSCodeshiftBackend())
