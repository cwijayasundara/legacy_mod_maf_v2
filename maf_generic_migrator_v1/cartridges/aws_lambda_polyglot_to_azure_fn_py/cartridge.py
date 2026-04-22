"""AWS Lambda (polyglot) -> Azure Functions (Python) cartridge.

Polyglot source: Python, Node.js, TypeScript, Java, C#.
Uniform target: Python Azure Functions v2 programming model.
"""
from __future__ import annotations

from pathlib import Path

from maf_generic_migrator_v1.platform_core.cartridge import (
    AgentSpec,
    IdiomMapping,
    MigrationCartridge,
    Rubric,
    ServiceMapping,
    TestFrameworkMapping,
)
from maf_generic_migrator_v1.platform_core.cartridge.ecosystem import EcosystemSignature
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Inventory
from maf_generic_migrator_v1.recipe_backends.base import Recipe

from maf_generic_migrator_v1.adapters.base import LanguageAdapter
from maf_generic_migrator_v1.adapters.csharp import CSharpAdapter
from maf_generic_migrator_v1.adapters.java import JavaAdapter
from maf_generic_migrator_v1.adapters.node import NodeAdapter
from maf_generic_migrator_v1.adapters.python import PythonAdapter
from maf_generic_migrator_v1.adapters.typescript import TypeScriptAdapter

from . import complexity_patterns
from .aws_sdk_patterns import resolve as resolve_sdk_call
from .iac_ingest import ingest_iac
from .idiom_map import IDIOM_MAP
from .service_map import SERVICE_MAP


class AwsLambdaPolyglotToAzureFnPy(MigrationCartridge):
    id = "aws_lambda_polyglot_to_azure_fn_py"
    description = "AWS Lambda (Python/Node/TS/Java/C#) -> Azure Functions (Python v2)"
    version = "0.1.0"

    source = EcosystemSignature(
        language="polyglot",
        runtime="aws-lambda",
        extra={"supported_source_languages": "python,node,typescript,java,csharp"},
    )
    target = EcosystemSignature(
        language="python",
        version="3.11",
        framework="azure-functions-v2",
        runtime="azure-fn",
    )

    # ---- Discovery ------------------------------------------------------- #

    def adapters(self) -> dict[str, LanguageAdapter]:
        return {
            "python": PythonAdapter(),
            "node": NodeAdapter(),
            "typescript": TypeScriptAdapter(),
            "java": JavaAdapter(),
            "csharp": CSharpAdapter(),
        }

    def unit_classifier(self, repo_root: Path) -> list[Path]:
        """One unit per Lambda handler.

        Detection is content-based (signature match), not filename-based,
        so sources like ``handlers/data_generator.py`` + ``handlers/filing_queue_status.py``
        become 4 separate units instead of one monolithic ``handlers/`` unit.

        Granularity rule:
          * If a directory contains multiple handler files → per-file units
            (each file is its own unit_root).
          * If a directory contains exactly one handler file → use the
            directory as the unit_root (preserves the legacy one-Lambda-per-dir
            layout, e.g. ``notification_sender/index.js``).
          * Java/C# stay directory-based (build manifests live alongside).
        """
        from collections import defaultdict
        import re as _re

        skip_dirs = {
            ".git", ".venv", "__pycache__", "node_modules", "dist", "build",
            ".pytest_cache", ".mypy_cache", "target",
        }

        def _scan_path(path: Path) -> bool:
            return not any(part in skip_dirs for part in path.parts)

        py_handler_rx = _re.compile(
            r"^\s*def\s+(?:lambda_)?handler\s*\(\s*event",
            _re.MULTILINE,
        )
        js_handler_rx = _re.compile(
            r"(?:exports|module\.exports)\.handler\s*=|"
            r"export\s+(?:const|async\s+function|function)\s+handler\b",
        )

        # --- 1. Python + JS/TS: scan files for handler signatures ---------- #
        py_handlers: list[Path] = []
        js_handlers: list[Path] = []

        for py in repo_root.rglob("*.py"):
            if not _scan_path(py) or py.name == "__init__.py":
                continue
            if py.name.startswith("test_") or py.stem.endswith("_test"):
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if py_handler_rx.search(text):
                py_handlers.append(py)

        for js_glob in ("*.js", "*.mjs", "*.cjs", "*.ts", "*.tsx"):
            for js in repo_root.rglob(js_glob):
                if not _scan_path(js):
                    continue
                try:
                    text = js.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if js_handler_rx.search(text):
                    js_handlers.append(js)

        # Group by parent dir: if a dir has multiple handlers, emit per-file
        # units; otherwise emit the parent dir.
        unit_roots: set[Path] = set()
        for handlers in (py_handlers, js_handlers):
            by_parent: defaultdict[Path, list[Path]] = defaultdict(list)
            for h in handlers:
                by_parent[h.parent].append(h)
            for parent, files in by_parent.items():
                if len(files) == 1:
                    unit_roots.add(parent)
                else:
                    unit_roots.update(files)

        # --- 2. Java (dir-based — pom.xml / build.gradle per module) ------- #
        for java_file in repo_root.rglob("*.java"):
            if not _scan_path(java_file):
                continue
            try:
                text = java_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "RequestHandler" in text or "RequestStreamHandler" in text:
                unit_roots.add(_java_module_root(java_file))

        # --- 3. C# (dir-based) -------------------------------------------- #
        for cs_file in repo_root.rglob("*.cs"):
            if not _scan_path(cs_file):
                continue
            try:
                text = cs_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "FunctionHandler" in text or "LambdaFunction" in text or "Amazon.Lambda.Core" in text:
                unit_roots.add(cs_file.parent)

        # --- 4. Fallback: top-level dirs if nothing matched ---------------- #
        if not unit_roots:
            for p in repo_root.iterdir():
                if p.is_dir() and not p.name.startswith(".") and p.name not in skip_dirs:
                    unit_roots.add(p)

        return sorted(unit_roots)

    def grapher_hints(self, inventory: Inventory) -> list[dict]:
        hints: list[dict] = list(ingest_iac(Path(inventory.repo_root)))
        # Enrich: for every resolvable SDK call, emit a typed unit->resource edge.
        for unit in inventory.units:
            for call in unit.service_calls:
                ref = resolve_sdk_call(call)
                if ref is None:
                    continue
                kind_to_edge = {
                    "reads": "reads_from",
                    "writes": "writes_to",
                    "produces": "queues_to",
                    "consumes": "reads_from",
                    "invokes": "invokes",
                }
                hints.append(
                    {
                        "source_unit": unit.unit_id,
                        "target_unit": None,
                        "target_resource": {
                            "kind": ref.kind.split("_")[1] if "_" in ref.kind else ref.kind,
                            "provider": "aws",
                            "name": ref.name,
                        },
                        "kind": kind_to_edge.get(ref.access, "invokes"),
                        "evidence": f"{call.file}:{call.line} {call.service}.{call.operation} ({ref.access})",
                        "confidence": 1.0 if ref.name else 0.7,
                    }
                )
        return hints

    def complexity_patterns(self, language: str) -> list[tuple[str, int, str]]:
        return complexity_patterns.patterns_for(language)

    def recipes(self) -> list[Recipe]:
        # Register the LibCST backend lazily so it self-registers with the
        # global registry on first call.
        import maf_generic_migrator_v1.recipe_backends.libcst_backend  # noqa: F401

        return [
            Recipe(
                id="cartridges.aws_lambda_polyglot_to_azure_fn_py.recipes.BotoImportToAzureStub",
                backend="libcst",
                languages=("python",),
                description="Remove boto3/botocore imports and add a banner for the LLM translator.",
            ),
        ]

    # ---- Transformation -------------------------------------------------- #

    def translator_agents(self) -> list[AgentSpec]:
        # No tools: the user message already contains full source + residuals,
        # and the output protocol is "emit fenced code blocks with path= headers"
        # (parsed by unit_worker._apply_translator_output). Exposing read_file /
        # glob / grep caused the agent to re-fetch files that were already in
        # the prompt, doubling the conversation size and tripping the model's
        # context window.
        return [
            AgentSpec(
                name=f"translator-{lang}",
                role="translator",
                system_prompt_path=f"prompts/translator_{lang}.md",
                tools=[],
            )
            for lang in ("python", "node", "typescript", "java", "csharp")
        ]

    def reviewer_agents(self) -> list[AgentSpec]:
        # Same rationale: the reviewer's prompt already includes every file in
        # the workdir via _render_source_files(show_generated=True). Tools
        # would only cause re-fetches.
        return [
            AgentSpec(
                name="reviewer",
                role="reviewer",
                system_prompt_path="prompts/reviewer.md",
                tools=[],
            )
        ]

    def tester_agents(self) -> list[AgentSpec]:
        # Tester keeps run_shell because it actually executes pytest; no need
        # for read_file since the prompt has the source.
        return [
            AgentSpec(
                name="tester",
                role="tester",
                system_prompt_path="prompts/tester.md",
                tools=["run_shell"],
            )
        ]

    def security_agents(self) -> list[AgentSpec]:
        return [
            AgentSpec(
                name="security",
                role="security",
                system_prompt_path="prompts/security.md",
                tools=[],
            )
        ]

    # ---- Mappings -------------------------------------------------------- #

    def service_map(self) -> list[ServiceMapping]:
        return SERVICE_MAP

    def idiom_map(self) -> list[IdiomMapping]:
        return IDIOM_MAP

    def test_framework_map(self) -> list[TestFrameworkMapping]:
        return [
            TestFrameworkMapping(source="pytest", target="pytest"),
            TestFrameworkMapping(
                source="mocha",
                target="pytest",
                assertion_map={"expect": "assert", "to.equal": "=="},
            ),
            TestFrameworkMapping(source="junit4", target="pytest"),
            TestFrameworkMapping(source="junit5", target="pytest"),
            TestFrameworkMapping(source="xunit", target="pytest"),
        ]

    # ---- Verification + Eval -------------------------------------------- #

    def rubrics(self) -> list[Rubric]:
        import yaml  # type: ignore[import-untyped]

        doc = yaml.safe_load((_HERE / "rubrics" / "translation.yaml").read_text())
        return [
            Rubric(id=r["id"], description=r["description"], weight=r["weight"], scorer=r["scorer"])
            for r in doc["rows"]
        ]

    def corpus_dir(self) -> Path | None:
        return _HERE / "corpus"

    def verify_unit(self, workdir: Path, item: BacklogItem) -> bool:
        """Run whatever tests exist under ``workdir``; treat "no tests" as soft-pass.

        The residual detector + reviewer already catch the no-output case; here
        we only want to fail the unit when tests *exist and fail*.
        """
        import logging

        from maf_generic_migrator_v1.platform_core.tools.test_runner import run_pytest

        logger = logging.getLogger(__name__)
        tests = list(workdir.rglob("test_*.py")) + list(workdir.rglob("*_test.py"))
        if not tests:
            logger.info("unit=%s: no tests present, skipping pytest", item.unit_id)
            return True
        result = run_pytest(workdir, timeout=180)
        if not result.passed:
            logger.warning(
                "unit=%s: pytest failed exit=%d\n%s",
                item.unit_id,
                result.exit_code,
                result.stdout[-1500:],
            )
        return result.passed

    def verify_wave(self, workdir: Path, graph, wave: int) -> bool:
        """Wave-level gate.

        * Probes the Azure emulator stack (Azurite + Service Bus + Cosmos).
        * If ``LEGACY_MOD_REQUIRE_EMULATORS`` is set, any missing emulator
          fails the wave. Otherwise soft-passes (status is logged).
        """
        import os

        from maf_generic_migrator_v1.platform_core.pipeline.verifier import wave_verification

        require = os.getenv("LEGACY_MOD_REQUIRE_EMULATORS", "").strip().lower()
        required: list[str] | None = None
        if require in {"1", "true", "yes"}:
            # Require Azurite + Service Bus; Cosmos is opt-in (large image).
            required = ["azurite-blob", "service-bus-emulator"]
        return wave_verification(required=required)


_HERE = Path(__file__).resolve().parent


def _java_module_root(java_file: Path) -> Path:
    """Walk up to the module root (directory containing pom.xml / build.gradle)."""
    for parent in java_file.parents:
        if (parent / "pom.xml").exists() or (parent / "build.gradle").exists() or (parent / "build.gradle.kts").exists():
            return parent
    return java_file.parent


# The loader looks for this symbol.
CARTRIDGE = AwsLambdaPolyglotToAzureFnPy()
