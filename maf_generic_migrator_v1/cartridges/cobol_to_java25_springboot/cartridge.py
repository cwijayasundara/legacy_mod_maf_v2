"""COBOL → Java 25 (LTS) / Spring Boot 4.0.5 cartridge.

Select via ``CARTRIDGE=cobol_to_java25_springboot`` in ``.env``.
Target toolchain: Java 25 LTS, Spring Boot 4.0.5
(Spring Framework 7, Jakarta EE 11), Maven 3.9.x.

Covers COBOL (fixed and free-form) + JCL + EXEC SQL (DB2) +
EXEC CICS, producing one Maven module per program with a
target style (``batch`` / ``cics`` / ``subroutine``) inferred
from the KG.
"""
from __future__ import annotations

import re
from pathlib import Path

from maf_generic_migrator_v1.adapters.base import LanguageAdapter
from maf_generic_migrator_v1.platform_core.cartridge import (
    AgentSpec,
    MigrationCartridge,
)
from maf_generic_migrator_v1.platform_core.cartridge.ecosystem import EcosystemSignature
from maf_generic_migrator_v1.platform_core.dualrun import (
    NullRunner,
    Runner,
    SubprocessRunner,
    Verdict,
    run_dual_run_from_dir,
)
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Inventory
from maf_generic_migrator_v1.platform_core.kg import KGStore
from maf_generic_migrator_v1.platform_core.tools.test_runner import run_maven

from .adapters.cobol import CobolAdapter
from .kg_extractors.jcl import ingest_jcl
from .target_style import classify_program
from .translator import _build_user_message as _build_cobol_translator_message
from maf_generic_migrator_v1.platform_core.comprehension.business_spec import (
    render_business_spec,
)
from maf_generic_migrator_v1.platform_core.pipeline.crud import build_crud_matrix

_HERE = Path(__file__).resolve().parent

#: Byte cap on the callee-API injection section. Typical callee has 1-3
#: Java files at ~2-5 KB each. A 40 KB cap comfortably fits the service
#: + all DTOs without crowding the translator's context budget.
_MAX_CALLEE_API_BYTES = 40_000

_PROGRAM_ID_SCAN = re.compile(
    r"^\s*PROGRAM-ID\s*\.\s*([A-Za-z][\w-]*)",
    re.IGNORECASE | re.MULTILINE,
)


class CobolToJava25SpringBoot(MigrationCartridge):
    id = "cobol_to_java25_springboot"
    description = "COBOL (batch + CICS + DB2) -> Java 25 (LTS) / Spring Boot 4.0.5"
    version = "0.1.0"

    source = EcosystemSignature(
        language="cobol",
        version="85",
        runtime="mvs",
        build_system="jcl",
        extra={"dialect": "ibm-enterprise"},
    )
    target = EcosystemSignature(
        language="java",
        version="25",
        framework="spring-boot-4.0",
        runtime="jvm",
        build_system="maven",
    )

    # ---- Discovery ----------------------------------------------------- #

    def adapters(self) -> dict[str, LanguageAdapter]:
        return {"cobol": CobolAdapter()}

    def unit_classifier(self, repo_root: Path) -> list[Path]:
        """One unit per COBOL program (identified by ``PROGRAM-ID``).

        Copybooks (``.cpy``) are context, not units. JCL and other artifacts
        are picked up by ``grapher_hints`` / ``ingest_kg_extras``, not here.
        """
        units: list[Path] = []
        for suffix in (".cbl", ".cob", ".cobol"):
            for path in repo_root.rglob(f"*{suffix}"):
                if any(part.startswith(".") for part in path.relative_to(repo_root).parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if _PROGRAM_ID_SCAN.search(text):
                    units.append(path)
        return sorted(units)

    def grapher_hints(self, inventory: Inventory) -> list[dict]:
        """Emit CALL-based cross-unit edges so the planner topologically
        orders callees BEFORE callers.

        Without these hints, the planner sees zero cross-unit dependencies
        and packs everything into a single wave. With them, programs that
        ``CALL 'X'`` end up in a later wave than X — so by the time the
        caller translates, X's Java is already written to its units/
        workdir and available for ``build_translator_request`` to inject
        as ground-truth API context.

        Extracted from ``external_call`` nodes in the KG. Each edge's
        ``external_call.name`` IS the CALLed PROGRAM-ID (see
        ``adapters/cobol.py::_add_regex_calls_and_exec_blocks``).
        """
        from pathlib import Path

        store = self._run_store(Path(inventory.repo_root))
        unit_ids = {u.unit_id for u in inventory.units}
        hints: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for program in store.iter_nodes(kind="program"):
            caller_id = program.name
            if caller_id not in unit_ids:
                continue
            # Walk every paragraph under this program; each ``calls`` edge
            # to an ``external_call`` node names the CALL target.
            for ext in store.iter_nodes(kind="external_call"):
                if not ext.id.startswith(f"external_call:{caller_id}:"):
                    continue
                target_id = ext.name
                if target_id not in unit_ids:
                    continue
                key = (caller_id, target_id)
                if key in seen:
                    continue
                seen.add(key)
                hints.append({
                    "source_unit": caller_id,
                    "target_unit": target_id,
                    "kind": "calls",
                    "evidence": f"CALL '{target_id}' — static KG edge",
                    "confidence": 1.0,
                })
        return hints

    def should_run_translator(
        self,
        item,
        workdir: Path,
        *,
        residuals,
        attempt: int = 0,
        reviewer_feedback: str = "",
    ) -> bool:
        """COBOL translates the whole source wholesale — always fire.

        The platform default is AWS-style ("only run when recipes left
        residuals"), which never applies here since COBOL has no
        recipe-emit-residual flow. Returning True also means the
        reviewer's revise verdicts still loop correctly (attempt > 0
        inherits the same True).
        """
        return True

    # ---- KG-aware translator driver ------------------------------------ #

    _RUN_KG_CACHE: dict[Path, KGStore] = {}  # repo_root → store

    def _run_store(self, repo_root: Path) -> KGStore:
        """Lazily build-and-cache the full KG for this repo.

        The workflow calls ``build_translator_request`` once per unit per
        attempt; building the KG anew each time would be wasteful and
        non-deterministic. Cache on the cartridge *class* keyed by
        ``repo_root`` so multiple units from the same repo share one
        populated graph.
        """
        from maf_generic_migrator_v1.platform_core.kg import NetworkXStore

        if repo_root in self._RUN_KG_CACHE:
            return self._RUN_KG_CACHE[repo_root]

        store = NetworkXStore()
        adapter = self.adapters()["cobol"]
        for unit_path in self.unit_classifier(repo_root):
            adapter.extract_kg(repo_root, unit_path, store)
        self.ingest_kg_extras(repo_root, store)
        self._RUN_KG_CACHE[repo_root] = store
        return store

    def _collect_migrated_callee_apis(
        self,
        cfg,             # WorkflowConfig
        store: KGStore,
        caller_id: str,
    ) -> str:
        """Render the Java source of every already-migrated callee into
        a verbatim prompt section for injection.

        Looks up ``external_call`` nodes under ``caller_id`` in the KG,
        computes the expected Java output path for each callee's service
        + DTO files under ``cfg.workdir/units/{target}/src/main/java/…``,
        and inlines them. Silently skips callees not yet migrated — safe
        fallback for corpora where the planner chose to run the caller
        in the same wave.

        Size-bounded so a huge library callee can't blow the prompt
        context.
        """
        from pathlib import Path

        from .translator import _java_names_for

        # Collect unique CALL targets from the KG.
        targets: list[str] = []
        for ext in store.iter_nodes(kind="external_call"):
            if not ext.id.startswith(f"external_call:{caller_id}:"):
                continue
            if ext.name not in targets:
                targets.append(ext.name)
        if not targets:
            return ""

        sections: list[str] = []
        injected_fqns: list[tuple[str, str]] = []  # (target_id, service FQN)
        total_bytes = 0
        for target_id in targets:
            names = _java_names_for(target_id)
            unit_root = Path(cfg.workdir) / "units" / target_id
            if not unit_root.is_dir():
                continue

            java_root = unit_root / "src" / "main" / "java" / names["package_path"]
            # The callee's service class — the primary entry point.
            svc_dir = java_root / "service"
            svc_files = sorted(svc_dir.glob("*.java")) if svc_dir.is_dir() else []
            dto_dir = java_root / "dto"
            dto_files = sorted(dto_dir.glob("*.java")) if dto_dir.is_dir() else []

            if not svc_files:
                continue

            class_base = names["class_base"]
            package = names["package"]
            svc_fqn = f"{package}.service.{class_base}Service"
            injected_fqns.append((target_id, svc_fqn))

            section_lines: list[str] = [
                f"### Callee `{target_id}` — migrated API (use EXACTLY these classes)",
                f"\nInject as: `import {svc_fqn};`  → field `private final {class_base}Service {class_base.lower()}Service;`",
            ]
            for path in svc_files + dto_files:
                size = path.stat().st_size
                if total_bytes + size > _MAX_CALLEE_API_BYTES:
                    section_lines.append(
                        f"(further callee files elided — prompt budget exhausted)"
                    )
                    break
                total_bytes += size
                rel = path.relative_to(unit_root)
                section_lines.append(
                    f"\n#### `{rel}`\n```java\n"
                    f"{path.read_text(encoding='utf-8', errors='replace')}\n```"
                )
            sections.append("\n".join(section_lines))

        if not sections:
            return ""

        # Enumerate the exact target IDs + FQNs in the header so the LLM
        # can't claim "I didn't know which callees to treat as already-
        # migrated" when deciding whether to emit a stub.
        injected_list = "\n".join(
            f"  * `{tid}` → inject `{fqn}` (NOT a local stub)"
            for tid, fqn in injected_fqns
        )

        header = (
            "## Already-migrated callee APIs (ground truth — use verbatim)\n"
            "\nThe programs you invoke via `CALL 'TARGET'` have already been\n"
            "translated to Java in an earlier wave. Their exact class names,\n"
            "method signatures, DTO types, and parameter orders are shown\n"
            "below. Your generated code MUST:\n"
            "\n"
            "- Inject the callee's ``@Service`` class by its exact name and\n"
            "  fully-qualified package path (shown per-callee below).\n"
            "- Call methods with the EXACT signature shown — no made-up\n"
            "  method names, no made-up parameter counts.\n"
            "- Use the exact DTO types listed for inputs / outputs — do NOT\n"
            "  invent parallel DTO classes with different names or fields.\n"
            "- Do NOT re-declare a stub interface or class for the callee;\n"
            "  the real service is on the classpath at integration time.\n"
            "\n"
            "**This OVERRIDES the generic \"ONE stub class per external\n"
            "CALL\" rule in the translator prompt.** For callees listed\n"
            "here, you emit ZERO stub classes — the real class is\n"
            "autowired by its injected package. Emitting a\n"
            "``*ServiceStub`` or a second copy of the service in the\n"
            "caller's own package is the ``duplicate-external-stub``\n"
            "reviewer finding and will fail review.\n"
            "\n"
            "### Callees already migrated (inject, don't stub):\n"
            f"{injected_list}\n"
        )
        return header + "\n\n".join(sections)

    async def build_translator_request(
        self,
        item,           # BacklogItem
        workdir: Path,
        *,
        cfg,            # WorkflowConfig
        agent_bundles: dict,
        residuals,
        reviewer_feedback: str = "",
        attempt: int = 0,
    ):
        """COBOL-specific translator wiring.

        * Populate the KG (cached per repo_root) so we can classify +
          render a data dictionary.
        * Classify this program's target style (batch / cics /
          subroutine) from KG signals — ``PROCEDURE DIVISION USING``,
          ``step_of`` edges, ``txn_call`` descendants.
        * Pick the matching ``translator-cobol-<style>`` agent.
        * Build the user message from ``translator._build_user_message``:
          business spec (LLM-summary-free for now — just the deterministic
          purpose/IO/side-effects blocks), data dictionary, and
          per-paragraph source chunks.
        * Pass reviewer feedback through when present so the retry loop
          still works.
        """
        store = self._run_store(cfg.repo_root)
        try:
            target_style = classify_program(store, item.unit_id)
        except KeyError:
            # Program not in KG — shouldn't happen for units returned by
            # unit_classifier, but fall back to the platform default.
            return None

        agent_name = f"translator-cobol-{target_style}"
        bundle = agent_bundles.get(agent_name)
        if bundle is None:
            # Agent wasn't materialized (shouldn't happen — all three
            # specs are declared in translator_agents). Fall back.
            return None

        matrix = build_crud_matrix(store)
        spec = render_business_spec(
            store, item.unit_id, matrix=matrix, target_style=target_style,
        )

        user_msg = _build_cobol_translator_message(
            spec, store, matrix, target_style=target_style,
        )

        # Inject the migrated API of any callee already translated in an
        # earlier wave. Without this, STMTGEN has to guess INTCALC's Java
        # method signatures and produces duplicate stub classes with
        # mismatched types. With it, STMTGEN sees INTCALC's real class +
        # method + DTO shapes verbatim and can call them correctly.
        callee_api = self._collect_migrated_callee_apis(cfg, store, item.unit_id)
        if callee_api:
            user_msg += "\n\n" + callee_api

        if reviewer_feedback:
            # Each retry re-emits the ENTIRE Maven module. Partial emission
            # breaks cross-file consistency: fixing one file but leaving
            # stale references to classes/methods the LLM quietly dropped
            # from the previous attempt is how we landed with compile
            # errors like ``cannot find symbol: class StatementRecord``.
            user_msg += (
                "\n\n## Reviewer feedback on the previous attempt\n"
                f"(retry {attempt}) — the reviewer found these issues:\n\n"
                f"{reviewer_feedback}\n\n"
                "**Re-emit the FULL Maven module** — every file from the\n"
                "required-deliverables list, with the fixes above applied.\n"
                "Keep the module self-consistent: every class referenced\n"
                "in one file MUST be defined in a file you also emit in\n"
                "this response. Every method called on a ``@Service`` or\n"
                "``@Component`` MUST be declared on that class."
            )
        return user_msg, bundle

    # ---- KG extras (called by Phase 1 tests and Phase 2 pipeline) ------ #

    def ingest_kg_extras(self, repo_root: Path, store: KGStore) -> None:
        """Populate the KG with cartridge-scoped extras beyond adapter output.

        Currently: JCL jobs/steps/datasets. Will grow to include DB2 schema
        DDL, CICS CSD parsing, and control-M / TWS schedules.
        """
        ingest_jcl(repo_root, store)

    # ---- Transformation (stubs until Phase 3) -------------------------- #

    def translator_agents(self) -> list[AgentSpec]:
        """Three target-style translators.

        The unit_worker / translator orchestrator selects which one fires
        per program via ``target_style.classify_program``. CICS wins over
        batch when both apply (mixed-mode estates); subroutine is the
        fallback when the program is neither JCL-invoked nor CICS-bound.
        """
        return [
            AgentSpec(
                name="translator-cobol-batch",
                role="translator",
                system_prompt_path="prompts/translator_batch.md",
                tools=[],
            ),
            AgentSpec(
                name="translator-cobol-cics",
                role="translator",
                system_prompt_path="prompts/translator_cics.md",
                tools=[],
            ),
            AgentSpec(
                name="translator-cobol-subroutine",
                role="translator",
                system_prompt_path="prompts/translator_subroutine.md",
                tools=[],
            ),
        ]

    def reviewer_agents(self) -> list[AgentSpec]:
        return [
            AgentSpec(
                name="reviewer",
                role="reviewer",
                system_prompt_path="prompts/reviewer.md",
                tools=[],
            )
        ]

    def tester_agents(self) -> list[AgentSpec]:
        return [
            AgentSpec(
                name="tester",
                role="tester",
                system_prompt_path="prompts/tester.md",
                tools=[],
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

    def comprehension_prompts_dir(self) -> Path:
        return _HERE / "prompts" / "comprehension"

    def verify_unit_with_feedback(
        self, workdir: Path, item: BacklogItem
    ) -> tuple[bool, str]:
        """Cartridge-specific richer verify that returns compile errors
        as feedback when they fail — so the translator retry loop can
        use them to fix cross-file issues mvn caught but the reviewer
        didn't (constructor arity, missing symbols, type mismatches).
        """
        from maf_generic_migrator_v1.platform_core.tools.test_runner import run_maven

        feedback_parts: list[str] = []
        if not (workdir / "pom.xml").is_file():
            # No pom.xml means the translator output was unparseable (e.g.
            # fenced blocks with an unsupported path-header convention).
            # Return failure + explicit feedback so the retry loop sees
            # the problem instead of soft-passing into a false success.
            has_response = (workdir / "translator_response.md").is_file()
            feedback_parts.append(
                "### Translator output not emitted as files\n"
                "No `pom.xml` is present in the workdir. The translator's "
                "response was captured as raw text but no fenced blocks "
                "were parsed into files. Most common causes:\n"
                "\n"
                "- The fenced blocks omitted a `path=...` header the parser\n"
                "  recognizes. Use ONE of: inline ``` ```xml path=pom.xml ```,\n"
                "  HTML comment ``<!-- path=pom.xml -->``, line comments\n"
                "  ``// path=X`` / ``# path=X``, or block comment ``/* path=X */``.\n"
                "- The response was prose, not fenced blocks at all.\n"
                "\n"
                "RE-EMIT the full Maven module as fenced blocks with `path=`\n"
                "headers. Every file the review requires must appear as its\n"
                "own fenced block, in the Java-naming section's prescribed\n"
                "paths."
            )
            if has_response:
                feedback_parts.append(
                    "The raw translator response is saved at "
                    "`translator_response.md` in this workdir for reference."
                )
            return False, "\n\n".join(feedback_parts)

        mvn = run_maven(workdir, goals=("-q", "compile"), timeout=600)
        if mvn.skipped:
            return True, ""
        if not mvn.passed:
            # Tail enough stderr to cite file:line but not blow the prompt.
            tail = (mvn.stdout + "\n" + mvn.stderr)[-3000:]
            feedback_parts.append(
                "### ``mvn compile`` failed — compile-error feedback\n"
                "Fix EVERY error listed below. If a class is missing, add\n"
                "the corresponding ``.java`` file; if a method is missing,\n"
                "declare it on the named class; if a constructor arity is\n"
                "wrong, declare the DTO as a ``record`` with the exact\n"
                "field list the caller uses.\n\n"
                "```\n"
                f"{tail}\n"
                "```"
            )
            return False, "\n\n".join(feedback_parts)

        # Compile passed — fall through to dualrun (may still skip/pass).
        verdict = _run_coro(self._run_dualrun(workdir, item))
        if verdict is None:
            return True, ""
        (workdir / "dualrun_verdict.md").write_text(
            verdict.render_markdown(), encoding="utf-8"
        )
        import os
        require_all = os.getenv("LEGACY_MOD_DUALRUN_REQUIRE", "").lower() in {"1", "true", "yes"}
        if verdict.errored > 0 or verdict.mismatched > 0:
            feedback_parts.append(
                f"### Dual-run failed for `{item.unit_id}` — "
                f"{verdict.mismatched} mismatched / {verdict.errored} errored\n"
                f"```\n{verdict.render_markdown()[:3000]}\n```"
            )
            return False, "\n\n".join(feedback_parts)
        if require_all and verdict.skipped > 0:
            return False, (
                f"### Dual-run skipped for `{item.unit_id}` (required by env)"
            )
        return True, ""

    def verify_unit(self, workdir: Path, item: BacklogItem) -> bool:
        """Two-stage gate: Maven compile, then dual-run equivalence.

        Stage 1 — ``mvn compile``. Hard-fails when Maven is installed and
        the generated Java doesn't compile. Soft-passes when Maven isn't
        on PATH (dev without a JDK).

        Stage 2 — dual-run equivalence. Runs every golden fixture for
        ``item.unit_id`` through the configured runner and fails when
        any fixture mismatches. Configured via env:

        * ``LEGACY_MOD_DUALRUN_CMD`` — space-separated argv template
          consumed by ``SubprocessRunner``. Absent → ``NullRunner``
          (every fixture is marked *skipped* in the verdict, run is
          still passed).
        * ``LEGACY_MOD_DUALRUN_REQUIRE=1`` — fail when any fixture is
          skipped (CI tightens the gate).

        Verdict markdown is written to ``workdir/dualrun_verdict.md``.
        """
        import logging
        import os

        logger = logging.getLogger(__name__)

        # -- Stage 1: Maven compile ------------------------------------ #
        if not (workdir / "pom.xml").is_file():
            logger.info("unit=%s: no pom.xml — translator did not run?", item.unit_id)
            return True
        mvn = run_maven(workdir, goals=("-q", "compile"), timeout=600)
        if mvn.skipped:
            logger.info("unit=%s: mvn gate skipped (%s)", item.unit_id, mvn.skipped_reason)
        elif not mvn.passed:
            logger.warning(
                "unit=%s: mvn compile failed exit=%d\n%s",
                item.unit_id,
                mvn.exit_code,
                (mvn.stdout + mvn.stderr)[-2000:],
            )
            return False

        # -- Stage 2: dual-run equivalence ----------------------------- #
        verdict = _run_coro(self._run_dualrun(workdir, item))
        if verdict is None:
            return True  # no fixtures for this unit; nothing to check
        (workdir / "dualrun_verdict.md").write_text(
            verdict.render_markdown(), encoding="utf-8"
        )
        require_all = os.getenv("LEGACY_MOD_DUALRUN_REQUIRE", "").lower() in {"1", "true", "yes"}
        if verdict.errored > 0 or verdict.mismatched > 0:
            logger.warning(
                "unit=%s: dualrun failed (matched=%d mismatched=%d errored=%d skipped=%d)",
                item.unit_id, verdict.matched, verdict.mismatched, verdict.errored, verdict.skipped,
            )
            return False
        if require_all and verdict.skipped > 0:
            logger.warning(
                "unit=%s: dualrun skips rejected by LEGACY_MOD_DUALRUN_REQUIRE",
                item.unit_id,
            )
            return False
        return True

    async def _run_dualrun(self, workdir: Path, item: BacklogItem) -> Verdict | None:
        """Load fixtures for the program named by ``item.unit_id`` and run them.

        Returns ``None`` when no fixtures exist — the unit has nothing to
        verify at the equivalence layer yet, which is the norm until a
        team has recorded its golden set.
        """
        fixtures_dir = self._fixtures_dir_for(item)
        if fixtures_dir is None or not fixtures_dir.is_dir():
            return None
        runner = self._build_dualrun_runner(workdir)
        return await run_dual_run_from_dir(item.unit_id, fixtures_dir, runner)

    def _fixtures_dir_for(self, item: BacklogItem) -> Path | None:
        """Convention: ``corpus/fixtures/<program-lower>/dualrun/``."""
        return _HERE / "corpus" / "fixtures" / item.unit_id.lower() / "dualrun"

    def _build_dualrun_runner(self, workdir: Path) -> Runner:
        import os
        import shlex

        cmd_template = os.getenv("LEGACY_MOD_DUALRUN_CMD", "").strip()
        if not cmd_template:
            return NullRunner(
                reason=(
                    "LEGACY_MOD_DUALRUN_CMD not set — fixtures loaded but not "
                    "executed. Set the env var to an argv template that takes "
                    "fixture inputs on stdin and emits outputs on stdout."
                )
            )
        return SubprocessRunner(command=shlex.split(cmd_template), workdir=workdir)

    def complexity_patterns(self, language: str) -> list[tuple[str, int, str]]:
        if language != "cobol":
            return []
        return [
            (r"\bALTER\b", 5, "ALTER statement"),
            (r"GO\s+TO\s+\w+\s+DEPENDING", 3, "Computed GO TO"),
            (r"PERFORM\s+\w+\s+THRU\s+\w+", 3, "PERFORM THRU"),
            (r"EXEC\s+CICS\s+(?:LINK|XCTL)", 3, "CICS program linkage"),
            (r"\bSORT\b.*\bUSING\b", 2, "SORT verb"),
            (r"COMP-3|PACKED-DECIMAL", 2, "Fixed-point arithmetic"),
            (r"REDEFINES", 2, "Memory redefinition"),
            (r"OCCURS.*DEPENDING ON", 2, "Variable-length table"),
            (r"EXEC\s+SQL", 1, "Embedded SQL"),
        ]


def _run_coro(coro):
    """Drive ``coro`` to completion whether or not we're already in a loop.

    ``asyncio.run`` refuses to run when a loop is already running (which
    happens when ``verify_unit`` is called from an async pipeline driver
    like the pilot runner). Fall back to a one-shot worker thread that
    has its own loop.
    """
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# Loader sentinel.
CARTRIDGE = CobolToJava25SpringBoot()
