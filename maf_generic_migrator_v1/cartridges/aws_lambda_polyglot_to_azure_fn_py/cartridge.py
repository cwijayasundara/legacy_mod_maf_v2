"""AWS Lambda (polyglot) -> Azure Functions (Python) cartridge.

Polyglot source: Python, Node.js, TypeScript, Java, C#.
Uniform target: Python Azure Functions v2 programming model.

Pipeline alignment: follows the platform-core contract
``source code -> AST -> LLM -> knowledge graph -> LLM -> migrated code``.
``extract_kg`` on each polyglot adapter projects the parsed AST into a
shared ``KGStore`` (program / paragraph / external_call / dataset_ref
nodes + contains / calls / reads / writes edges). The cartridge's
``build_translator_request`` renders a KG-derived business spec and
inlines it into the translator prompt alongside the seeded residuals,
so the LLM translator consumes KG facts — not just raw source.
"""
from __future__ import annotations

import logging
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
from maf_generic_migrator_v1.platform_core.comprehension import (
    ChatClient,
    SummaryCache,
    run_comprehension,
)
from maf_generic_migrator_v1.platform_core.comprehension.business_spec import (
    render_business_spec,
)
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Inventory
from maf_generic_migrator_v1.platform_core.kg import KGStore, render_graphs
from maf_generic_migrator_v1.platform_core.pipeline.crud import build_crud_matrix
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

logger = logging.getLogger(__name__)


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

    # ---- Knowledge graph (platform-core pipeline stage 2) --------------- #
    #
    #   source code -> AST (adapters) -> KG (this section) -> LLM
    #   comprehension / business spec -> LLM translator -> migrated code.
    #
    # The adapters' ``extract_kg`` already projects each unit's AST into
    # ``program`` / ``paragraph`` / ``external_call`` / ``dataset_ref``
    # nodes. This cartridge caches the populated store per repo_root so
    # every unit in a run shares one KG, then ``build_translator_request``
    # renders a business spec for the current unit and injects it into the
    # translator prompt.

    _RUN_KG_CACHE: dict[Path, KGStore] = {}
    _RUN_IR_CACHE: dict[Path, dict[str, "UnitIRDump"]] = {}
    _RUN_ARTIFACTS_WRITTEN: set[Path] = set()
    _RUN_COMPREHENSION_DONE: set[Path] = set()

    def _run_store(self, repo_root: Path) -> KGStore:
        """Build (or return the cached) KGStore for ``repo_root``.

        Runs every cartridge adapter's ``extract_kg`` hook against every
        unit returned by ``unit_classifier``, then layers on cartridge
        extras (IaC-provided resources) via ``ingest_kg_extras``. Also
        captures each unit's flat ``UnitIR`` for later persistence as the
        "AST output" artifact (imports + service-call sites with file /
        line granularity).
        """
        from maf_generic_migrator_v1.platform_core.kg import NetworkXStore

        cached = self._RUN_KG_CACHE.get(repo_root)
        if cached is not None:
            return cached

        store = NetworkXStore()
        ir_by_unit: dict[str, UnitIRDump] = {}
        adapters = self.adapters()
        for unit_root in self.unit_classifier(repo_root):
            adapter = _pick_adapter_for_unit(unit_root, adapters)
            if adapter is None:
                continue
            try:
                ir = adapter.extract_unit(repo_root, unit_root)
                ir_by_unit[ir.unit_id] = UnitIRDump(
                    unit_id=ir.unit_id,
                    language=ir.language,
                    root_path=ir.root_path,
                    handler_entry=ir.handler_entry,
                    files=list(ir.files),
                    imports=[imp.model_dump() for imp in ir.imports],
                    service_calls=[sc.model_dump() for sc in ir.service_calls],
                    loc=ir.loc,
                    byte_size=ir.byte_size,
                )
                adapter.extract_kg(repo_root, unit_root, store)
            except Exception as exc:  # noqa: BLE001
                # Never fail the run over a single unit's KG extract —
                # comprehension / spec rendering degrades gracefully when
                # a program node is missing. Log and move on.
                logger.info(
                    "extract_kg skipped for %s (%s): %s",
                    unit_root, adapter.language, exc,
                )
        self.ingest_kg_extras(repo_root, store)
        self._RUN_KG_CACHE[repo_root] = store
        self._RUN_IR_CACHE[repo_root] = ir_by_unit
        return store

    def persist_kg_artifacts(self, repo_root: Path, workdir: Path) -> None:
        """Write KG + AST + graph renderings to ``workdir`` (idempotent).

        Layout::

            <workdir>/
              kg.json                     # full graph snapshot (nodes + edges)
              ast/<unit_id>.json          # per-unit flat IR — "AST output"
              graphs/call_graph.{dot,mmd,svg?}
              graphs/dataflow.{dot,mmd,svg?}

        Runs once per (repo_root, workdir) pair; subsequent calls short-
        circuit so the checkpoint driver can call this unconditionally
        on every unit without incurring duplicate work.
        """
        key = workdir.resolve()
        if key in self._RUN_ARTIFACTS_WRITTEN:
            return
        try:
            store = self._run_store(repo_root)
        except Exception as exc:  # noqa: BLE001
            logger.info("KG persistence skipped (build failed): %s", exc)
            return

        workdir.mkdir(parents=True, exist_ok=True)

        # Full graph snapshot — the "knowledge graph" artifact.
        try:
            (workdir / "kg.json").write_text(
                store.snapshot().model_dump_json(indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("kg.json snapshot skipped: %s", exc)

        # Per-unit IR snapshots — the "AST output" artifact.
        import json

        ast_dir = workdir / "ast"
        ast_dir.mkdir(parents=True, exist_ok=True)
        for unit_id, dump in self._RUN_IR_CACHE.get(repo_root, {}).items():
            safe = _safe_filename(unit_id)
            (ast_dir / f"{safe}.json").write_text(
                json.dumps(dump.to_dict(), indent=2), encoding="utf-8"
            )

        # Mermaid + DOT (+ SVG via graphviz when available). Platform
        # helper emits both diagrams the dependency-graph viewer expects.
        try:
            render_graphs(store, workdir / "graphs")
        except Exception as exc:  # noqa: BLE001
            logger.info("graph rendering skipped: %s", exc)

        self._RUN_ARTIFACTS_WRITTEN.add(key)
        logger.info(
            "KG artifacts persisted under %s (kg.json, ast/, graphs/)", workdir,
        )

    def ingest_kg_extras(self, repo_root: Path, store: KGStore) -> None:
        """Seed the KG with dataset_ref nodes from IaC (SAM / serverless / CDK).

        Without this, resources declared in infrastructure-as-code but
        never touched by an SDK call wouldn't appear in the KG. Included
        here so the same KG drives both the dependency graph (already
        done via ``grapher_hints``) and the comprehension / business-spec
        layer.
        """
        from maf_generic_migrator_v1.platform_core.kg import KGNode

        for hint in ingest_iac(repo_root):
            target_resource = hint.get("target_resource") if isinstance(hint, dict) else None
            if not target_resource:
                continue
            kind = target_resource.get("kind") if isinstance(target_resource, dict) else None
            name = target_resource.get("name") if isinstance(target_resource, dict) else None
            if not kind:
                continue
            node_id = f"dataset_ref:{kind}:{name or '_unnamed'}"
            if store.has_node(node_id):
                continue
            attrs = {"resource_kind": kind, "origin": "iac"}
            if name:
                attrs["resource_name"] = name
            store.add_node(
                KGNode(
                    id=node_id,
                    kind="dataset_ref",
                    name=name or kind,
                    attributes=attrs,
                )
            )

    def comprehension_prompts_dir(self) -> Path:
        """Point the comprehension summarizer at Lambda-tailored prompts.

        The summarizer searches cartridge prompts first, platform defaults
        second. These prompts bias summaries toward Lambda-shaped idioms
        (handler signature, trigger types, SDK call sites) so the program-
        level ``llm_summary`` block the business-spec renderer parses is
        structured around AWS-native concepts even when the source is
        Python / Node / Java / C#.
        """
        return _HERE / "prompts" / "comprehension"

    async def build_translator_request(
        self,
        item,              # BacklogItem
        workdir: Path,
        *,
        cfg,               # WorkflowConfig
        agent_bundles: dict,
        residuals,
        reviewer_feedback: str = "",
        attempt: int = 0,
    ):
        """Inject the KG-derived business spec into the default AWS translator prompt.

        Returns ``(user_message, agent_bundle)`` so ``unit_worker`` uses
        this instead of its built-in path. We keep the built-in message
        body (residuals + source + required-deliverables) and prepend the
        business spec, so the LLM sees *both* the concrete source to
        translate *and* the KG summary of what the Lambda does. Returning
        ``None`` (the base-class default) would drop us back to the
        source-only path — losing the KG signal we just built.
        """
        from maf_generic_migrator_v1.platform_core.maf_workflows.unit_worker import (
            _build_translator_message,
            _detect_item_language,
            _LANG_TO_TRANSLATOR,
        )

        lang = _detect_item_language(item, cfg.repo_root)
        agent_name = _LANG_TO_TRANSLATOR.get(lang)
        bundle = agent_bundles.get(agent_name) if agent_name else None
        if bundle is None:
            bundle = next(
                (b for b in agent_bundles.values() if b.role == "translator"),
                None,
            )
        if bundle is None:
            return None

        user_msg = _build_translator_message(
            item, residuals, workdir,
            reviewer_feedback=reviewer_feedback, attempt=attempt,
        )

        # Persist run-scope artifacts once per run (KG snapshot + AST +
        # mermaid/DOT). The workdir root is the MAF cfg.workdir — the
        # same place ``checkpoints/`` and ``cost_reports/`` already land,
        # so the review UI finds everything in one place.
        self.persist_kg_artifacts(cfg.repo_root, cfg.workdir)

        # Run bottom-up LLM comprehension so the business spec's Purpose
        # / Paragraphs sections render with real distilled prose instead
        # of "(purpose not yet summarized)". Idempotent per run via
        # ``_RUN_COMPREHENSION_DONE``; subsequent units in the same run
        # reuse the populated KG summaries.
        await self._ensure_comprehension_done(cfg)

        spec_block, spec_markdown = self._render_spec_block_and_markdown(
            cfg.repo_root, item.unit_id
        )
        if spec_markdown:
            self._persist_unit_spec(cfg.workdir, workdir, item.unit_id, spec_markdown)
        if spec_block:
            user_msg = spec_block + "\n\n" + user_msg
        return user_msg, bundle

    async def _ensure_comprehension_done(self, cfg) -> None:  # noqa: ANN001
        """Run the LLM comprehension pass once per run.

        Populates ``llm_summary`` on every KG node so
        ``render_business_spec`` can fill in Purpose + per-paragraph
        summaries. Bounded by:

        * ``LEGACY_MOD_COMPREHENSION_MAX_SUMMARIES`` — hard cap on fresh
          LLM calls this pass issues. 0 or unset disables comprehension
          (cartridge falls back to deterministic-only spec).
        * ``LEGACY_MOD_COMPREHENSION_SAMPLE_PROGRAMS`` — only summarize
          the top-N programs by centrality + their descendants. Critical
          for 100k+ LOC estates.

        Persists the ``SummaryCache`` to
        ``<cfg.workdir>/comprehension_cache.jsonl`` so re-runs re-use
        per-node summaries for unchanged source (content-hash keyed).
        """
        import os

        workdir_key = Path(cfg.workdir).resolve()
        if workdir_key in self._RUN_COMPREHENSION_DONE:
            return

        max_summaries_raw = os.getenv("LEGACY_MOD_COMPREHENSION_MAX_SUMMARIES", "")
        max_summaries: int | None
        try:
            max_summaries = (
                int(max_summaries_raw) if max_summaries_raw.strip() else None
            )
        except ValueError:
            max_summaries = None
        if max_summaries is not None and max_summaries <= 0:
            logger.info(
                "comprehension disabled via LEGACY_MOD_COMPREHENSION_MAX_SUMMARIES=%s",
                max_summaries_raw,
            )
            self._RUN_COMPREHENSION_DONE.add(workdir_key)
            return

        sample_raw = os.getenv("LEGACY_MOD_COMPREHENSION_SAMPLE_PROGRAMS", "")
        sample_programs: int | None
        try:
            sample_programs = int(sample_raw) if sample_raw.strip() else None
        except ValueError:
            sample_programs = None

        try:
            store = self._run_store(cfg.repo_root)
        except Exception as exc:  # noqa: BLE001
            logger.info("comprehension skipped (KG build failed): %s", exc)
            self._RUN_COMPREHENSION_DONE.add(workdir_key)
            return

        chat_client = _build_comprehension_chat_client()
        if chat_client is None:
            logger.info("comprehension skipped: no chat client available")
            self._RUN_COMPREHENSION_DONE.add(workdir_key)
            return

        cache_path = Path(cfg.workdir) / "comprehension_cache.jsonl"
        logger.info(
            "comprehension: starting (max_summaries=%s, sample_programs=%s, cache=%s)",
            max_summaries, sample_programs, cache_path,
        )
        try:
            result = await run_comprehension(
                store,
                self,
                chat_client=chat_client,
                cache_path=cache_path,
                max_summaries=max_summaries,
                sample_programs=sample_programs,
            )
            logger.info(
                "comprehension: %d node summaries produced this run",
                result.summarized_nodes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("comprehension run failed: %s", exc)

        self._RUN_COMPREHENSION_DONE.add(workdir_key)

    def _persist_unit_spec(
        self,
        run_workdir: Path,
        unit_workdir: Path,
        unit_id: str,
        spec_markdown: str,
    ) -> None:
        """Drop the business spec in two places for easy review.

        * ``<run_workdir>/business_specs/<unit>.md`` — the central
          index every reviewer already scans (matches the COBOL pilot).
        * ``<unit_workdir>/business_spec.md`` — colocated with the
          migrated code so a single-unit diff carries its spec.
        """
        try:
            specs_dir = run_workdir / "business_specs"
            specs_dir.mkdir(parents=True, exist_ok=True)
            safe = _safe_filename(unit_id)
            (specs_dir / f"{safe}.md").write_text(spec_markdown, encoding="utf-8")
            unit_workdir.mkdir(parents=True, exist_ok=True)
            (unit_workdir / "business_spec.md").write_text(
                spec_markdown, encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("business-spec persistence skipped for %s: %s", unit_id, exc)

    def _render_spec_block(self, repo_root: Path, unit_id: str) -> str:
        """Prompt-ready spec block (shim kept for the existing tests)."""
        block, _ = self._render_spec_block_and_markdown(repo_root, unit_id)
        return block

    def _render_spec_block_and_markdown(
        self, repo_root: Path, unit_id: str
    ) -> tuple[str, str]:
        """Return ``(prompt_block, raw_markdown)`` for ``unit_id``.

        Both degrade gracefully to empty strings when the KG has no
        program node for this unit — protects us from hard-failing when
        a new polyglot adapter hasn't implemented ``extract_kg`` yet.
        """
        try:
            store = self._run_store(repo_root)
        except Exception as exc:  # noqa: BLE001
            logger.info("KG build skipped for %s: %s", unit_id, exc)
            return "", ""
        try:
            matrix = build_crud_matrix(store)
            spec = render_business_spec(store, unit_id, matrix=matrix)
        except KeyError:
            return "", ""
        except Exception as exc:  # noqa: BLE001
            logger.info("business-spec rendering skipped for %s: %s", unit_id, exc)
            return "", ""

        raw = spec.markdown.rstrip() + "\n"
        # The spec already has ``# Program Spec: <id>`` as its H1. Wrap
        # it for the LLM prompt with a short framing header so the model
        # understands it's ground truth distilled from the KG rather
        # than more raw source to translate.
        wrapped = (
            "## KG-derived business spec (ground truth — use to preserve behaviour)\n"
            "\nThe section below is rendered from the knowledge graph the\n"
            "platform built from this Lambda's AST (inputs/outputs from\n"
            "CRUD analysis, side-effect call sites from `external_call`\n"
            "nodes, paragraph-level summaries when present). Treat the\n"
            "Inputs / Outputs / Side-effects enumeration as authoritative:\n"
            "the migrated Azure Function must preserve every dataset and\n"
            "call site listed, remapped to its Azure equivalent per the\n"
            "service map.\n"
            "\n"
            f"{spec.markdown.rstrip()}"
        )
        return wrapped, raw

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


from dataclasses import asdict, dataclass, field  # noqa: E402


@dataclass
class UnitIRDump:
    """Serializable flat snapshot of one unit's ``UnitIR``.

    Written per unit under ``<workdir>/ast/<unit_id>.json`` so the
    dependency-graph / diff viewers have a single-file view of each
    Lambda's imports and SDK call sites with file + line granularity.
    The KG snapshot in ``kg.json`` carries the same information cross-
    referenced by graph edges; this per-unit form is the AST-flat
    projection most tooling finds easier to diff.
    """

    unit_id: str
    language: str
    root_path: str
    handler_entry: str | None
    files: list[str]
    imports: list[dict]
    service_calls: list[dict]
    loc: int
    byte_size: int

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_filename(unit_id: str) -> str:
    """Sanitize a unit id for use as a POSIX filename.

    ``unit_id`` can contain ``/`` / ``\\`` / ``.`` in multi-level
    package paths (e.g. ``handlers.data_generator``). Keep dots since
    they're legal on every filesystem we target, but replace separators
    with ``_`` so nothing escapes the target directory.
    """
    import re

    return re.sub(r"[^A-Za-z0-9._-]", "_", unit_id) or "unit"


# --------------------------------------------------------------------------- #
# MAF chat client -> comprehension ChatClient adapter
# --------------------------------------------------------------------------- #


class _MAFComprehensionClient:
    """Adapt a MAF ``ChatClient`` to the summarizer's ``ChatClient`` protocol.

    The summarizer expects ``await client.chat(system=..., user=...) -> str``.
    MAF clients expose ``await client.get_response(messages, options=...) ->
    ChatResponse``. This adapter bridges them and also funnels token-usage
    into the run-wide ``BudgetTracker`` so comprehension cost lands in the
    same cost report as translators / reviewers.
    """

    def __init__(self, client, model: str | None = None) -> None:  # noqa: ANN001
        self._client = client
        self._model = model or getattr(client, "model", None)

    async def chat(self, *, system: str, user: str) -> str:
        from agent_framework import ChatOptions, Message

        messages = [
            Message(role="system", contents=[system]),
            Message(role="user", contents=[user]),
        ]
        # Summaries are short by design (3-5 sentences). 2000 tokens is
        # plenty of headroom and keeps per-call cost predictable.
        options = ChatOptions(max_tokens=2000)
        response = await self._client.get_response(messages, options=options)

        # Feed usage into the run-wide BudgetTracker if one is active.
        try:
            from maf_generic_migrator_v1.platform_core.maf_workflows.budget import (
                current as current_budget,
            )

            budget = current_budget()
        except Exception:  # noqa: BLE001
            budget = None
        if budget is not None:
            usage = getattr(response, "usage_details", None) or getattr(
                response, "usage", None
            )
            if usage is not None:
                in_tok = _read_usage(usage, "input_token_count", "input_tokens", "prompt_tokens")
                out_tok = _read_usage(usage, "output_token_count", "output_tokens", "completion_tokens")
                if in_tok or out_tok:
                    budget.record(
                        model=self._model,
                        in_tokens=in_tok,
                        out_tokens=out_tok,
                        tag="comprehension",
                    )

        text = getattr(response, "text", None)
        if text:
            return text
        messages_out = getattr(response, "messages", None)
        if messages_out:
            return "\n".join(getattr(m, "text", str(m)) for m in messages_out)
        return str(response)


def _read_usage(obj, *keys) -> int:  # noqa: ANN001
    for k in keys:
        if isinstance(obj, dict):
            v = obj.get(k)
        else:
            v = getattr(obj, k, None)
        if v is not None:
            return int(v)
    return 0


def _build_comprehension_chat_client() -> ChatClient | None:
    """Lazy-build a chat client for comprehension.

    Reuses the same provider-selection logic as the translator agents
    (``build_chat_client`` reads ``LEGACY_MOD_PROVIDER`` / credentials),
    so comprehension automatically follows whichever model the rest of
    the run is using. Returns ``None`` on any credential / import
    failure so the caller falls back to the deterministic-only spec.
    """
    try:
        from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import (
            build_chat_client,
        )

        maf_client = build_chat_client()
    except Exception as exc:  # noqa: BLE001
        logger.info("comprehension chat client unavailable: %s", exc)
        return None
    return _MAFComprehensionClient(maf_client)


#: Maps the unit-root extension / layout signal back to an adapter. Used by
#: ``_run_store`` when iterating over ``unit_classifier`` output — each
#: unit carries a different source language since this cartridge is
#: polyglot. Java / C# ship as directories; the file-based heuristics
#: fall back to directory scans.
_SUFFIX_TO_LANGUAGE = {
    ".py": "python",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".cs": "csharp",
}


def _pick_adapter_for_unit(
    unit_root: Path, adapters: dict[str, LanguageAdapter]
) -> LanguageAdapter | None:
    """Return the adapter whose language matches this unit's sources.

    File units route by suffix. Directory units scan the contents: pick
    the adapter whose supported suffixes match files found under the
    root. Prefer Java / C# when build manifests are present so a mixed
    directory doesn't misroute to Node.
    """
    if unit_root.is_file():
        lang = _SUFFIX_TO_LANGUAGE.get(unit_root.suffix.lower())
        return adapters.get(lang) if lang else None

    if (unit_root / "pom.xml").exists() or (unit_root / "build.gradle").exists():
        return adapters.get("java")
    if any(unit_root.glob("*.csproj")):
        return adapters.get("csharp")

    for suffix, lang in _SUFFIX_TO_LANGUAGE.items():
        if any(unit_root.rglob(f"*{suffix}")):
            return adapters.get(lang)
    return None


def _java_module_root(java_file: Path) -> Path:
    """Walk up to the module root (directory containing pom.xml / build.gradle)."""
    for parent in java_file.parents:
        if (parent / "pom.xml").exists() or (parent / "build.gradle").exists() or (parent / "build.gradle.kts").exists():
            return parent
    return java_file.parent


# The loader looks for this symbol.
CARTRIDGE = AwsLambdaPolyglotToAzureFnPy()
