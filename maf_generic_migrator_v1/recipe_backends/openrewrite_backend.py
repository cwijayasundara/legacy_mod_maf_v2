"""OpenRewrite bridge (JVM).

Invokes OpenRewrite via Maven or Gradle subprocess. Assumes the project under
``workdir`` has a build file and that OpenRewrite is wired in (``rewrite-maven-plugin``
or ``rewrite-gradle-plugin``).

Recipe ids are OpenRewrite recipe fully qualified names (e.g.
``org.openrewrite.java.migrate.UpgradeToJava22``).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .base import Recipe, RecipeBackend, RecipeResult, register_backend


class OpenRewriteBackend(RecipeBackend):
    id = "openrewrite"
    supported_languages = ("java", "kotlin", "groovy")

    def available(self) -> bool:
        return shutil.which("mvn") is not None or shutil.which("gradle") is not None

    def apply(self, recipe: Recipe, source_paths: list[str], workdir: Path) -> RecipeResult:
        build_file = _find_build_file(workdir)
        if build_file is None:
            return RecipeResult(
                recipe_id=recipe.id,
                backend=self.id,
                applied=False,
                error=f"no pom.xml or build.gradle in {workdir}",
            )

        cmd = _build_command(build_file, recipe)
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            return RecipeResult(
                recipe_id=recipe.id,
                backend=self.id,
                applied=False,
                error="openrewrite timed out after 600s",
            )

        applied = proc.returncode == 0 and "CHANGED" in (proc.stdout + proc.stderr)
        return RecipeResult(
            recipe_id=recipe.id,
            backend=self.id,
            applied=applied,
            files_changed=[],           # OpenRewrite writes in place; diff externally
            diagnostics=[proc.stdout.splitlines()[-20:][-1]] if proc.stdout else [],
            error=None if proc.returncode == 0 else f"exit={proc.returncode}: {proc.stderr[-400:]}",
        )


def _find_build_file(workdir: Path) -> Path | None:
    for name in ("pom.xml", "build.gradle", "build.gradle.kts"):
        candidate = workdir / name
        if candidate.is_file():
            return candidate
    return None


def _build_command(build_file: Path, recipe: Recipe) -> list[str]:
    if build_file.name == "pom.xml":
        return ["mvn", "-q", "rewrite:run", f"-Drewrite.activeRecipes={recipe.id}"]
    return ["gradle", "-q", "rewriteRun", f"-PactiveRecipe={recipe.id}"]


register_backend(OpenRewriteBackend())
