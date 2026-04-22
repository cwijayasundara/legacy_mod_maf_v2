"""Migration cartridge: the pluggable unit of migration-pair-specific behavior.

A cartridge is loaded by id. The platform core knows nothing about a specific
source/target pair; it dispatches every stage through the cartridge interface.

Cartridges live under ``maf_generic_migrator_v1/cartridges/<id>/cartridge.py`` and declare a module-level
``CARTRIDGE`` symbol (an instance of ``MigrationCartridge``).
"""
from __future__ import annotations

import importlib
import importlib.util
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from .ecosystem import EcosystemSignature

if TYPE_CHECKING:
    from maf_generic_migrator_v1.adapters.base import LanguageAdapter
    from maf_generic_migrator_v1.recipe_backends.base import Recipe, RecipeBackend

    from maf_generic_migrator_v1.platform_core.ir import BacklogItem, DependencyGraph, Inventory


class ServiceMapping(BaseModel):
    """One row in a cartridge's service/API map.

    e.g. ``ServiceMapping(source_kind='sqs', target_kind='service-bus-queue')``
    """

    source_kind: str
    source_operation: str | None = None
    target_kind: str
    target_operation: str | None = None
    notes: str | None = None


class IdiomMapping(BaseModel):
    """A named idiom rewrite the cartridge performs.

    Idioms are usually expressed as recipes when they're mechanical. IdiomMapping
    entries are used to generate prompt fragments for the LLM translator when the
    rewrite needs judgment.
    """

    name: str
    source_pattern: str
    target_pattern: str
    explanation: str


class TestFrameworkMapping(BaseModel):
    """How source test framework fixtures map to target test framework."""

    source: str                 # 'junit4' | 'mocha' | 'pytest' | 'xunit' | ...
    target: str
    assertion_map: dict[str, str] = Field(default_factory=dict)
    setup_teardown_map: dict[str, str] = Field(default_factory=dict)


class BuildSystemMapping(BaseModel):
    """How source build-system manifest translates to target."""

    source: str                 # 'maven' | 'npm' | 'msbuild' | 'setuptools' | ...
    target: str
    manifest_transformations: list[str] = Field(default_factory=list)  # recipe ids applied to manifests


class Rubric(BaseModel):
    """One scoring rubric row used to evaluate a migration result."""

    id: str
    description: str
    weight: float = 1.0
    scorer: str = Field(..., description="Dotted path of a scorer callable, e.g. 'my_cartridge.scorers.tests_pass'")


class AgentSpec(BaseModel):
    """Declarative agent definition the MAF workflow materializes.

    Keeps cartridges decoupled from the MAF imports so they can be unit-tested
    without an MAF runtime.
    """

    name: str
    role: str                   # 'translator' | 'reviewer' | 'tester' | 'security' | 'critic'
    system_prompt_path: str
    tools: list[str] = Field(default_factory=list)
    model: str | None = None
    max_turns: int = 10
    temperature: float | None = None


class MigrationCartridge(ABC):
    """Abstract cartridge.

    A concrete cartridge subclasses this, declares metadata via properties, and
    exposes its mappings, recipes, translators, agents, and corpus via hooks.
    """

    id: str
    source: EcosystemSignature
    target: EcosystemSignature
    description: str = ""
    version: str = "0.1.0"

    # ---- Discovery ------------------------------------------------------- #

    @abstractmethod
    def adapters(self) -> dict[str, LanguageAdapter]:
        """Language-name -> adapter. A polyglot cartridge returns multiple."""

    @abstractmethod
    def unit_classifier(self, repo_root: Path) -> list[Path]:
        """Return the root directories that should each be treated as one unit.

        For a single-Lambda repo → one entry. For a multi-Lambda monorepo →
        one entry per Lambda. For a Python-package repo → one entry per module.
        """

    def grapher_hints(self, inventory: Inventory) -> list[dict]:
        """Optional: cartridge-specific hints for cross-unit edge detection.

        e.g. the AWS cartridge ingests SAM/serverless/CDK files and returns
        resource -> lambda_id mappings as edge seeds.
        """
        return []

    # ---- Transformation -------------------------------------------------- #

    def recipes(self) -> list[Recipe]:
        """Deterministic rewrites applied before any LLM call."""
        return []

    def recipe_backends(self) -> list[RecipeBackend]:
        """The backends a cartridge's recipes rely on (OpenRewrite, LibCST, ...)."""
        return []

    @abstractmethod
    def translator_agents(self) -> list[AgentSpec]:
        """LLM translators (one per source-language slice, typically)."""

    def reviewer_agents(self) -> list[AgentSpec]:
        return []

    def tester_agents(self) -> list[AgentSpec]:
        return []

    def security_agents(self) -> list[AgentSpec]:
        return []

    # ---- Mappings -------------------------------------------------------- #

    def service_map(self) -> list[ServiceMapping]:
        return []

    def idiom_map(self) -> list[IdiomMapping]:
        return []

    def test_framework_map(self) -> list[TestFrameworkMapping]:
        return []

    def build_system_map(self) -> list[BuildSystemMapping]:
        return []

    # ---- Verification + Eval -------------------------------------------- #

    def rubrics(self) -> list[Rubric]:
        return []

    def corpus_dir(self) -> Path | None:
        """Directory containing golden fixtures, if any."""
        return None

    def should_run_translator(
        self,
        item,          # BacklogItem
        workdir: Path,
        *,
        residuals,
        attempt: int = 0,
        reviewer_feedback: str = "",
    ) -> bool:
        """Decide whether the translator should fire this iteration.

        Default matches the AWS-Lambda flow: only run when recipes left
        residuals unresolved, OR when the reviewer asked for revisions.
        Cartridges with no "recipes clean up most of the file" pattern
        (COBOL → Spring Boot, etc.) should override to always-true.
        """
        return bool(getattr(residuals, "empty", True) is False) or bool(reviewer_feedback)

    async def build_translator_request(
        self,
        item,          # BacklogItem
        workdir: Path,
        *,
        cfg,           # WorkflowConfig
        agent_bundles: dict,
        residuals,
        reviewer_feedback: str = "",
        attempt: int = 0,
    ):
        """Optional hook — cartridges can take full control of translator
        agent selection AND the user-message content.

        Return ``(user_message, agent_bundle)`` to override; return
        ``None`` to fall back to ``unit_worker``'s default AWS-Lambda
        flow (residual-based message, source-language-based agent
        selection). The default implementation returns ``None``.

        Introduced so cartridges whose target shape is NOT Azure
        Functions (COBOL→Spring Boot, PL/I→Kotlin, etc.) can bypass the
        built-in AWS-specific prompt without the platform having to
        encode every target style natively.
        """
        return None

    def comprehension_prompts_dir(self) -> Path | None:
        """Optional: cartridge-specific comprehension-prompt overrides.

        When set, the comprehension pipeline searches this dir first for
        ``<node_kind>.md`` and falls back to platform defaults. Used by
        legacy-language cartridges (COBOL, PL/I) to bias summaries toward
        language-specific idioms (PIC clauses, COMP-3, CICS verbs) without
        rewriting the default prompt chain.
        """
        return None

    def verify_unit(self, workdir: Path, item: BacklogItem) -> bool:
        """Run the cartridge's unit-level verification. Default: pass."""
        return True

    def verify_unit_with_feedback(
        self, workdir: Path, item: BacklogItem
    ) -> tuple[bool, str]:
        """Like ``verify_unit`` but also returns human-readable feedback.

        Called from the translator retry loop: when verification fails but
        retries remain, the returned ``feedback`` string is fed back to
        the translator as if the reviewer had asked for a revision.
        Default: delegates to ``verify_unit`` and returns an empty
        feedback string (so a cartridge that hasn't opted in has no
        retry effect beyond the bool).

        Cartridges that want compile-error / test-failure feedback into
        the retry loop override this to return the captured stderr /
        stdout tail when verification fails.
        """
        return self.verify_unit(workdir, item), ""

    def verify_wave(self, workdir: Path, graph: DependencyGraph, wave: int) -> bool:
        """Cross-unit integration verification at wave boundary. Default: pass."""
        return True


# ------------------------------------------------------------------------- #
# Registry / loader
# ------------------------------------------------------------------------- #

_CARTRIDGE_MODULE_ATTR = "CARTRIDGE"


def load_cartridge_by_id(cartridge_id: str, cartridges_root: Path | None = None) -> MigrationCartridge:
    """Import ``maf_generic_migrator_v1/cartridges/<id>/cartridge.py`` and return its ``CARTRIDGE`` symbol.

    Prefer the regular import machinery when the cartridge package is on
    ``sys.path`` — this ensures ``inspect.getfile`` works on the cartridge
    class (which the conformance tests rely on). Fall back to a file-spec
    import for ad-hoc cartridge paths.
    """

    cartridges_root = cartridges_root or (Path(__file__).resolve().parents[2] / "cartridges")
    path = cartridges_root / cartridge_id / "cartridge.py"
    if not path.is_file():
        raise FileNotFoundError(f"No cartridge.py at {path}")

    module_name = f"maf_generic_migrator_v1.cartridges.{cartridge_id}.cartridge"
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        import sys as _sys

        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create import spec for {path}") from None
        module = importlib.util.module_from_spec(spec)
        _sys.modules[module_name] = module
        spec.loader.exec_module(module)

    cartridge = getattr(module, _CARTRIDGE_MODULE_ATTR, None)
    if not isinstance(cartridge, MigrationCartridge):
        raise TypeError(f"{path} does not export a MigrationCartridge named {_CARTRIDGE_MODULE_ATTR!r}")
    return cartridge


def list_cartridges(cartridges_root: Path | None = None) -> list[str]:
    """Enumerate cartridge ids available on disk."""

    cartridges_root = cartridges_root or (Path(__file__).resolve().parents[2] / "cartridges")
    if not cartridges_root.is_dir():
        return []
    return sorted(
        p.name
        for p in cartridges_root.iterdir()
        if p.is_dir() and (p / "cartridge.py").is_file()
    )
