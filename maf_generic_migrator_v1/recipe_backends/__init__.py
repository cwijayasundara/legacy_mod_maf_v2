"""Bridges to deterministic code-rewriting engines.

Recipes are per-backend; the platform supplies a uniform Recipe type + an
application driver. Backends: OpenRewrite (JVM), LibCST (Python),
jscodeshift (JS/TS), Roslyn (C#), sqlglot (SQL), tree-sitter (legacy).
"""
from .base import Recipe, RecipeBackend, RecipeResult, apply_recipes

__all__ = ["Recipe", "RecipeBackend", "RecipeResult", "apply_recipes"]
