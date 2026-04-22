"""Per-unit composite worker.

Flow:
  1. Apply cartridge recipes (deterministic, no LLM).
  2. Diff recipe output; compute residuals (AWS SDK remnants, handler-shape etc.).
  3. Invoke translator agent on residuals.
  4. Reviewer agent validates; cheap retry loop on ``revise`` verdict.
  5. Tester agent runs unit tests.
  6. Security agent scans.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge
from maf_generic_migrator_v1.platform_core.ir import BacklogItem

from .budget import current as current_budget
from .checkpoints import UnitResult

if TYPE_CHECKING:
    from .agent_factory import AgentBundle
    from .main_workflow import WorkflowConfig

logger = logging.getLogger(__name__)


@dataclass
class Residuals:
    """What the recipes could not fix — fed to the translator."""

    aws_imports: list[tuple[str, int, str]]  # (file, line, raw_line)
    aws_sdk_calls: list[tuple[str, int, str]]
    handler_shape_needs_rewrite: bool
    files_without_azure: list[str]

    @property
    def empty(self) -> bool:
        return (
            not self.aws_imports
            and not self.aws_sdk_calls
            and not self.handler_shape_needs_rewrite
            and not self.files_without_azure
        )

    def to_markdown(self, repo_root: Path) -> str:
        lines = []
        if self.aws_imports:
            lines.append("### Unresolved AWS imports")
            for f, ln, raw in self.aws_imports[:30]:
                lines.append(f"- `{f}:{ln}` `{raw.strip()}`")
        if self.aws_sdk_calls:
            lines.append("\n### Unresolved AWS SDK calls")
            for f, ln, raw in self.aws_sdk_calls[:30]:
                lines.append(f"- `{f}:{ln}` `{raw.strip()}`")
        if self.handler_shape_needs_rewrite:
            lines.append("\n### Handler shape must be rewritten to Azure Functions v2 Python decorators.")
        if self.files_without_azure:
            lines.append("\n### Files missing Azure SDK imports:")
            for f in self.files_without_azure[:20]:
                lines.append(f"- `{f}`")
        return "\n".join(lines) or "(none)"


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


async def run_unit_async(
    item: BacklogItem,
    cartridge: MigrationCartridge,
    cfg: "WorkflowConfig",
    agent_bundles: "dict[str, AgentBundle] | None" = None,
    max_reviewer_retries: int = 2,
) -> UnitResult:
    """Execute one unit of migration."""
    result = UnitResult(unit_id=item.unit_id, wave=item.wave, status="running")
    workdir = _unit_workdir(cfg, item)
    workdir.mkdir(parents=True, exist_ok=True)
    repo_root = cfg.repo_root

    # Seed workdir with a copy of the original unit source so agents edit in place.
    _seed_workdir(repo_root, workdir, item)

    # --- Phase 1: deterministic recipes ---------------------------------- #
    result.recipes_applied = await _apply_recipes(cartridge, item, workdir)

    # --- Phase 2: diff residuals ---------------------------------------- #
    residuals = compute_residuals(workdir, item)
    logger.info(
        "unit=%s recipes=%d residuals(empty=%s aws_imp=%d aws_calls=%d handler=%s)",
        item.unit_id,
        len(result.recipes_applied),
        residuals.empty,
        len(residuals.aws_imports),
        len(residuals.aws_sdk_calls),
        residuals.handler_shape_needs_rewrite,
    )

    # --- Phases 3+4: translator/reviewer loop --------------------------- #
    #   Loop: translator -> reviewer. On "revise", feed findings back as
    #   extra instructions and try again, up to max_reviewer_retries.
    reviewer_verdict: str | None = None
    reviewer_feedback: str = ""
    last_verify_passed: bool = True          # default-pass if the loop breaks
    last_verify_feedback: str = ""           # before verify runs (e.g. skipped)
    # Snapshot of the last workdir state that passed verify — used to
    # roll back when a later retranslation regresses compile/tests.
    last_good_snapshot: dict[str, bytes] | None = None
    attempt = 0
    while True:
        # The cartridge decides whether to fire the translator this pass.
        # Default (AWS flow): only when recipes left residuals unresolved
        # or the reviewer asked for revisions. COBOL overrides to always
        # True because the whole source is being translated wholesale.
        should_translate = bool(agent_bundles) and cartridge.should_run_translator(
            item, workdir,
            residuals=residuals, attempt=attempt, reviewer_feedback=reviewer_feedback,
        )
        if should_translate:
            await _run_translator(
                cartridge,
                item,
                residuals,
                workdir,
                repo_root,
                agent_bundles,
                result,
                cfg=cfg,
                reviewer_feedback=reviewer_feedback,
                attempt=attempt,
            )
            # The translator has emitted Azure replacements. Remove the
            # original AWS source files from the workdir so the reviewer (and
            # the residual scanner on the next loop) only sees migrated code.
            # Without this, files like aws_clients.py and helper.py remain in
            # their boto3 form and the reviewer correctly rejects with
            # "still imports boto3".
            _purge_originals(workdir, item)

        if agent_bundles:
            reviewer_verdict, reviewer_feedback = await _run_reviewer(
                item, workdir, agent_bundles, result
            )
        else:
            reviewer_verdict = "skipped"
            break

        # Run verify on EVERY iteration and merge its feedback with the
        # reviewer's. Earlier design only ran verify when the reviewer
        # accepted — but when the reviewer keeps asking for revision
        # (stylistic findings), compile-only errors never entered the
        # feedback stream and the final attempt still shipped broken
        # Java. Combining both signals lets the translator see static
        # + compile issues simultaneously.
        verify_passed, verify_feedback = cartridge.verify_unit_with_feedback(
            workdir, item
        )
        last_verify_passed = verify_passed
        last_verify_feedback = verify_feedback
        if verify_passed:
            # Capture a byte-level snapshot of the current workdir so we
            # can roll back if a later retranslation regresses compile.
            # Retranslation is inherently lossy — the LLM sometimes
            # "fixes" the reviewer's stylistic finding and breaks a
            # working compile in the same breath. Preserve the last
            # known-good output.
            last_good_snapshot = _snapshot_workdir(workdir)
            logger.info(
                "unit=%s verify=pass at attempt %d — snapshot preserved",
                item.unit_id, attempt,
            )

        # The compile/test gate is the authoritative signal. LLM
        # reviewers hallucinate (phantom "file missing" claims for
        # files that clearly exist, subroutine rules misapplied to
        # CICS targets, etc.). When verify says "passes", downgrade a
        # reviewer reject into an advisory revise so the loop can use
        # the remaining retry budget to address any legitimate
        # concerns without aborting the unit outright.
        if reviewer_verdict == "reject" and verify_passed:
            logger.info(
                "unit=%s reviewer rejected but verify passed — "
                "downgrading to revise (compiler is authoritative)",
                item.unit_id,
            )
            reviewer_verdict = "revise"

        # Reject-with-verify-fail is genuinely unsalvageable; break.
        if reviewer_verdict == "reject":
            break
        # Fully clean (reviewer happy AND verify passed) — break.
        if reviewer_verdict != "revise" and verify_passed:
            break
        # Out of retry budget — break and let the caller record the state.
        if attempt >= max_reviewer_retries:
            break

        # Continue with combined feedback for the next translator attempt.
        combined: list[str] = []
        if reviewer_verdict == "revise" and reviewer_feedback:
            combined.append("### Reviewer findings\n" + reviewer_feedback)
        if not verify_passed and verify_feedback:
            combined.append(verify_feedback)
        reviewer_feedback = "\n\n---\n\n".join(combined) if combined else reviewer_feedback

        attempt += 1
        logger.info(
            "unit=%s retry %d/%d — reviewer=%s verify=%s",
            item.unit_id,
            attempt,
            max_reviewer_retries,
            reviewer_verdict,
            "pass" if verify_passed else "fail",
        )
        # Refresh residuals since the translator has written new files.
        residuals = compute_residuals(workdir, item)

    # --- Rollback on final regression ----------------------------------- #
    # If the loop exited with a failing verify but an earlier iteration
    # HAD passed, restore the last-known-good workdir. Prevents the LLM
    # from shipping a broken final attempt when a previous attempt was
    # compiling cleanly.
    if not last_verify_passed and last_good_snapshot is not None:
        logger.info(
            "unit=%s rolling back to last verify=pass snapshot "
            "(%d files) — current attempt regressed",
            item.unit_id, len(last_good_snapshot),
        )
        _restore_workdir(workdir, last_good_snapshot)
        last_verify_passed = True
        last_verify_feedback = ""

    result.reviewer_verdict = reviewer_verdict or "skipped"
    if reviewer_verdict == "reject":
        result.error = f"reviewer rejected: {reviewer_feedback[:200]}"

    # --- Phase 5: tester ------------------------------------------------ #
    # Reuse the verify result captured inside the loop — running it again
    # would repeat expensive work (mvn compile, dualrun) for no new signal.
    # When the loop never ran verify (e.g. the ``skipped`` path where no
    # agents were available), ``last_verify_passed`` defaults to True.
    if reviewer_verdict == "skipped":
        result.tests_passed = cartridge.verify_unit(workdir, item)
    else:
        result.tests_passed = last_verify_passed
        if last_verify_feedback:
            # Persist the last compile/test feedback so the checkpoint
            # carries it (useful for post-run triage).
            (workdir / "verify_feedback.md").write_text(
                last_verify_feedback, encoding="utf-8"
            )

    # --- Phase 6: security --------------------------------------------- #
    if agent_bundles:
        await _run_security(item, workdir, agent_bundles, result)

    # --- Phase 7: cartridge rubric scoring ----------------------------- #
    try:
        from maf_generic_migrator_v1.eval_harness.rubric_runner import run_rubric

        rubric_report = run_rubric(cartridge, workdir, item)
        result.rubric_score = rubric_report.total_score
        (workdir / "rubric_report.json").write_text(
            _rubric_report_to_json(rubric_report),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("rubric run skipped: %s", exc)

    if result.tests_passed and not result.error and reviewer_verdict != "reject":
        result.status = "succeeded"
    else:
        result.status = "failed"
        if not result.error and not result.tests_passed:
            result.error = "unit tests failed"

    return result


def _rubric_report_to_json(report) -> str:  # noqa: ANN001
    import json

    payload = {
        "cartridge": report.cartridge_id,
        "unit": report.unit_id,
        "total_score": round(report.total_score, 3),
        "passed": report.passed(),
        "rows": [
            {"id": r.rubric_id, "weight": r.weight, "score": round(r.score, 3), "error": r.error}
            for r in report.rows
        ],
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _unit_workdir(cfg: "WorkflowConfig", item: BacklogItem) -> Path:
    return cfg.workdir / "units" / item.unit_id


def _seed_workdir(repo_root: Path, workdir: Path, item: BacklogItem) -> None:
    """Copy the unit's source files into the workdir (idempotent)."""
    for src in item.source_paths:
        src_path = repo_root / src
        if not src_path.is_file():
            continue
        dest = workdir / Path(src).name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest)


#: Directories we never include in snapshots or rollback scope. Compile
#: / build artifacts regenerate; VCS + dependency trees are never ours.
_SNAPSHOT_SKIP_DIRS: set[str] = {
    "target", "build", "node_modules", ".git", ".venv",
    "__pycache__", ".pytest_cache", ".mypy_cache",
}


def _snapshot_workdir(workdir: Path) -> dict[str, bytes]:
    """Capture every non-build file under ``workdir`` as ``{rel_path: bytes}``.

    Intentionally byte-level: LLM-generated text files are small (usually
    <50KB per Maven module), and raw bytes sidestep any encoding quirks
    that might corrupt round-trip. ``target/`` and friends are excluded
    — they regenerate on the next ``mvn compile`` and would balloon the
    snapshot.
    """
    out: dict[str, bytes] = {}
    for p in workdir.rglob("*"):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(workdir).parts
        if any(part in _SNAPSHOT_SKIP_DIRS for part in rel_parts):
            continue
        out[str(p.relative_to(workdir))] = p.read_bytes()
    return out


def _restore_workdir(workdir: Path, snapshot: dict[str, bytes]) -> None:
    """Restore ``workdir`` to the contents captured in ``snapshot``.

    Removes any non-snapshot file under ``workdir`` (except the skipped
    build dirs) so the state is exactly what the snapshot recorded —
    not a merge. Then writes the snapshot bytes.

    Idempotent: calling twice with the same snapshot produces the same
    result. Safe to call when ``workdir`` has target/ build/ etc. from
    an intervening ``mvn compile`` — those aren't touched.
    """
    snapshot_paths = {workdir / rel for rel in snapshot}
    for p in list(workdir.rglob("*")):
        if not p.is_file():
            continue
        rel_parts = p.relative_to(workdir).parts
        if any(part in _SNAPSHOT_SKIP_DIRS for part in rel_parts):
            continue
        if p not in snapshot_paths:
            try:
                p.unlink()
            except OSError:
                pass
    for rel, content in snapshot.items():
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


def _purge_originals(workdir: Path, item: BacklogItem) -> None:
    """Delete files in workdir whose basenames match the original source files,
    UNLESS the translator overwrote them with a migrated version this turn.

    The translator can either:
      * emit a fenced block with the SAME path (overwrites the original — keep)
      * emit a fenced block with a NEW path (the original is now stale — purge)

    Heuristic: a file is "stale" if it still contains a top-level ``boto3`` /
    ``botocore`` / ``aws-sdk`` import OR a ``def (lambda_)?handler(event,``
    signature. Those are the markers of an unmigrated AWS Lambda file.
    """
    src_names = {Path(p).name for p in item.source_paths}
    aws_marker = re.compile(
        r"^\s*(?:import\s+boto3|from\s+boto3|import\s+botocore|from\s+botocore|"
        r"@aws-sdk/|"
        r"def\s+(?:lambda_)?handler\s*\(\s*event)",
        re.MULTILINE,
    )
    purged: list[str] = []
    for p in workdir.glob("*"):
        if not p.is_file() or p.name not in src_names:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if aws_marker.search(text):
            try:
                p.unlink()
                purged.append(p.name)
            except OSError:
                pass
    if purged:
        logger.info("unit=%s purged unmigrated originals: %s", item.unit_id, purged)


async def _apply_recipes(
    cartridge: MigrationCartridge, item: BacklogItem, workdir: Path
) -> list[str]:
    recipes = cartridge.recipes()
    if not recipes:
        return []
    from maf_generic_migrator_v1.recipe_backends.base import apply_recipes

    applied: list[str] = []
    for r in apply_recipes(recipes, [str(workdir / Path(p).name) for p in item.source_paths], workdir):
        if r.applied:
            applied.append(r.recipe_id)
        if r.error:
            logger.info("recipe %s did not apply: %s", r.recipe_id, r.error)
    return applied


# --------------------------------------------------------------------------- #
# Residuals
# --------------------------------------------------------------------------- #


_AWS_IMPORT_RX = re.compile(
    r"(?:^\s*import\s+boto3\b|^\s*from\s+boto3|"
    r"^\s*import\s+botocore|^\s*from\s+botocore|"
    r"@aws-sdk/|"
    r"software\.amazon\.awssdk|"
    r"com\.amazonaws|"
    r"Amazon\.[A-Z]\w+\.)",
    re.MULTILINE,
)
_AWS_CALL_RX = re.compile(
    r"\b(?:boto3\.(?:client|resource)\(|"
    r"new\s+(?:SQS|SNS|S3|DynamoDB|Kinesis|Lambda|SFN|EventBridge)Client\b|"
    r"(?:Sqs|Sns|S3|DynamoDb|Kinesis|Lambda|Sfn|EventBridge)Client\.builder\b|"
    r"new\s+Amazon[A-Z]\w+Client\b)"
)
_AZURE_IMPORT_RX = re.compile(r"(?:^\s*(?:from|import)\s+azure\.|from\s+azure\.)", re.MULTILINE)
_AZURE_V2_DECORATOR_RX = re.compile(r"@app\.(?:route|service_bus_queue_trigger|event_grid_trigger|blob_trigger|timer_trigger|schedule)")
_LAMBDA_HANDLER_RX = re.compile(r"def\s+lambda_handler\s*\(|exports\.handler\s*=|RequestHandler<|FunctionHandler\b")


_AZURE_FN_FILE_MARKERS = ("function_app.py", "requirements.txt")


def compute_residuals(workdir: Path, item: BacklogItem) -> Residuals:
    """Scan ``workdir`` (the full unit output tree) for AWS remnants.

    The translator writes *new* target files (function_app.py etc.) alongside
    the original source; residuals therefore need to consider both the target
    tree (is an Azure Function present?) and the seeded originals (are any
    AWS remnants still shipping?).
    """
    aws_imports: list[tuple[str, int, str]] = []
    aws_calls: list[tuple[str, int, str]] = []
    files_without_azure: list[str] = []
    has_v2_decorator = False
    has_lambda_shape = False

    has_azure_function = (workdir / "function_app.py").is_file()

    scanned_extensions = {".py", ".js", ".mjs", ".ts", ".java", ".cs"}
    for p in sorted(workdir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in scanned_extensions:
            continue
        if any(part in {"node_modules", ".venv", "__pycache__", "target"} for part in p.parts):
            continue

        rel = str(p.relative_to(workdir))
        text = p.read_text(encoding="utf-8", errors="replace")
        # Only flag residuals on files we expect to remove/replace (the
        # originals) or on the freshly-written function_app.py.
        is_original = p.name not in _AZURE_FN_FILE_MARKERS
        if is_original:
            for i, line in enumerate(text.splitlines(), start=1):
                if _AWS_IMPORT_RX.search(line):
                    aws_imports.append((rel, i, line))
                if _AWS_CALL_RX.search(line):
                    aws_calls.append((rel, i, line))
        if _AZURE_V2_DECORATOR_RX.search(text):
            has_v2_decorator = True
        if _LAMBDA_HANDLER_RX.search(text) and p.name == "function_app.py":
            has_lambda_shape = True
        if p.suffix == ".py" and p.name == "function_app.py" and not _AZURE_IMPORT_RX.search(text):
            files_without_azure.append(rel)

    # If an Azure Function entry file exists and looks right, clear the AWS
    # remnants from the "originals" — those files will be discarded after
    # migration.
    if has_azure_function and has_v2_decorator and not has_lambda_shape:
        aws_imports = []
        aws_calls = []

    handler_shape_needs_rewrite = has_lambda_shape or (not has_v2_decorator and not has_azure_function)
    return Residuals(
        aws_imports=aws_imports,
        aws_sdk_calls=aws_calls,
        handler_shape_needs_rewrite=handler_shape_needs_rewrite,
        files_without_azure=files_without_azure,
    )


# --------------------------------------------------------------------------- #
# Agent invocations
# --------------------------------------------------------------------------- #


_LANG_TO_TRANSLATOR = {
    "python": "translator-python",
    "node": "translator-node",
    "typescript": "translator-typescript",
    "java": "translator-java",
    "csharp": "translator-csharp",
}


async def _run_translator(
    cartridge: MigrationCartridge,
    item: BacklogItem,
    residuals: Residuals,
    workdir: Path,
    repo_root: Path,
    agents: "dict[str, AgentBundle]",
    result: UnitResult,
    *,
    cfg: "WorkflowConfig",
    reviewer_feedback: str = "",
    attempt: int = 0,
) -> None:
    # First give the cartridge a chance to fully override both (a) which
    # translator agent fires and (b) the user-message content. Cartridges
    # whose target isn't Azure Functions (COBOL→Spring Boot, etc.) use
    # this to bypass the AWS-specific defaults below.
    override = await cartridge.build_translator_request(
        item, workdir,
        cfg=cfg, agent_bundles=agents,
        residuals=residuals, reviewer_feedback=reviewer_feedback, attempt=attempt,
    )
    if override is not None:
        user_msg, bundle = override
        if bundle is None:
            logger.info("cartridge returned override but agent bundle was None (unit=%s)", item.unit_id)
            return
    else:
        lang = _detect_item_language(item, repo_root)
        agent_name = _LANG_TO_TRANSLATOR.get(lang)
        bundle = agents.get(agent_name) if agent_name else None
        if bundle is None:
            bundle = next((b for b in agents.values() if b.role == "translator"), None)
        if bundle is None:
            logger.info("no translator agent available for %s (lang=%s)", item.unit_id, lang)
            return
        user_msg = _build_translator_message(
            item, residuals, workdir,
            reviewer_feedback=reviewer_feedback, attempt=attempt,
        )

    tag = f"translator[{attempt}]" if attempt else "translator"
    response_text = await _invoke_agent(bundle, user_msg, result, tag=tag)
    _apply_translator_output(response_text, workdir, result)


async def _run_reviewer(
    item: BacklogItem, workdir: Path, agents: "dict[str, AgentBundle]", result: UnitResult
) -> tuple[str, str]:
    """Run the reviewer; return ``(verdict, feedback_markdown)``.

    Verdict is one of: ``accept`` / ``revise`` / ``reject`` / ``unknown``.
    """
    bundle = next((b for b in agents.values() if b.role == "reviewer"), None)
    if bundle is None:
        return "unknown", ""
    user_msg = _build_reviewer_message(item, workdir)
    response_text = await _invoke_agent(bundle, user_msg, result, tag="reviewer")
    result.reviewer_notes = response_text[:4000]
    return _parse_reviewer_verdict(response_text)


async def _run_security(
    item: BacklogItem, workdir: Path, agents: "dict[str, AgentBundle]", result: UnitResult
) -> None:
    bundle = next((b for b in agents.values() if b.role == "security"), None)
    if bundle is None:
        return
    user_msg = _build_security_message(item, workdir)
    response_text = await _invoke_agent(bundle, user_msg, result, tag="security")
    result.security_findings = _parse_security_findings(response_text)


# Role-keyed max_tokens budgets. Without these, Anthropic's wrapper falls back
# to its own ``ANTHROPIC_DEFAULT_MAX_TOKENS = 1024``, which truncates translators
# mid-class (observed: ~906 avg out tokens per Sonnet call, emitting only
# ``pom.xml + Application.java + one DTO`` per unit). Translators need room for
# a full Spring Boot module (pom + app + service + DTOs + controller + tests +
# README); reviewers/security/tester stay modest since their outputs are JSON
# verdicts or short findings. OpenAI wrappers also honour this and cap cleanly
# at the model's own ceiling, so a single value works across providers.
_MAX_TOKENS_BY_ROLE: dict[str, int] = {
    "translator": 16000,
    "reviewer": 8000,
    "security": 4000,
    "tester": 4000,
}
_DEFAULT_MAX_TOKENS = 4000


def _chat_options_for(bundle: "AgentBundle"):
    """ChatOptions with a role-appropriate ``max_tokens``.

    Lazily imports ``ChatOptions`` so test environments without MAF still load.
    """
    from agent_framework import ChatOptions

    role = getattr(bundle.spec, "role", "") or ""
    budget_tokens = _MAX_TOKENS_BY_ROLE.get(role, _DEFAULT_MAX_TOKENS)
    return ChatOptions(max_tokens=budget_tokens)


async def _invoke_agent(
    bundle: "AgentBundle", user_message: str, result: UnitResult, *, tag: str
) -> str:
    # Budget guard: skip this (and any subsequent) LLM call for the unit if
    # the run-wide tracker says we've already hit ``max_budget_usd``.
    budget = current_budget()
    if budget is not None and budget.should_halt:
        logger.warning("%s skipped — LLM budget exhausted ($%.4f / $%.2f)",
                       tag, budget.used_usd, budget.max_usd)
        if not result.error:
            result.error = f"budget exhausted before {tag} (${budget.used_usd:.4f} / ${budget.max_usd:.2f})"
        return ""
    try:
        response = await bundle.agent.run(user_message, options=_chat_options_for(bundle))
    except Exception as exc:  # noqa: BLE001 — one agent failure must not kill the workflow
        logger.exception("%s agent failed", tag)
        result.error = f"{tag} agent error: {exc}"
        return ""
    text = _response_text(response)
    _record_usage(response, result, tag, bundle=bundle, budget=budget)
    return text


def _response_text(response) -> str:  # noqa: ANN001
    # MAF AgentResponse exposes .text; fall back to str() if shape differs.
    text = getattr(response, "text", None)
    if text:
        return text
    messages = getattr(response, "messages", None)
    if messages:
        return "\n".join(getattr(m, "text", str(m)) for m in messages)
    return str(response)


def _record_usage(response, result: UnitResult, tag: str, *, bundle=None, budget=None) -> None:  # noqa: ANN001
    """Extract token usage from an MAF ``AgentResponse`` and feed the tracker.

    MAF's ``UsageDetails`` is a ``TypedDict`` with keys ``input_token_count`` /
    ``output_token_count`` / ``total_token_count`` — NOT attribute-style
    access and NOT the OpenAI-native ``input_tokens`` names.
    """
    usage = getattr(response, "usage_details", None)
    if usage is None:
        # Some paths expose the raw openai usage object with different keys.
        usage = getattr(response, "usage", None)
        if usage is None:
            return

    def _read(obj, *keys):
        for k in keys:
            if isinstance(obj, dict):
                v = obj.get(k)
            else:
                v = getattr(obj, k, None)
            if v is not None:
                return int(v)
        return 0

    in_tokens = _read(usage, "input_token_count", "input_tokens", "prompt_tokens")
    out_tokens = _read(usage, "output_token_count", "output_tokens", "completion_tokens")
    total = _read(usage, "total_token_count", "total_tokens") or (in_tokens + out_tokens)

    for key, val in (
        (f"{tag}.input_tokens", in_tokens),
        (f"{tag}.output_tokens", out_tokens),
        (f"{tag}.total_tokens", total),
    ):
        if val:
            result.tokens[key] = result.tokens.get(key, 0) + val

    if budget is not None and (in_tokens or out_tokens):
        # Prefer the model the client was actually built with (falls back to
        # env vars in ``build_chat_client``). Going through ``spec.model``
        # alone leaves it None whenever the cartridge's AgentSpec omits
        # ``model=``, which is the common case — that is why the ``by-model``
        # report was collapsing everything under ``unknown``.
        model = getattr(bundle, "resolved_model", None) or getattr(
            getattr(bundle, "spec", None), "model", None
        )
        # Populate the per-unit cost_usd field on UnitResult so per-unit cost
        # is visible in the checkpoint without opening the global report.
        result.cost_usd += budget.cost_of(
            model=model, in_tokens=in_tokens, out_tokens=out_tokens
        )
        budget.record(model=model, in_tokens=in_tokens, out_tokens=out_tokens, tag=tag)


# --------------------------------------------------------------------------- #
# Message builders
# --------------------------------------------------------------------------- #


def _build_translator_message(
    item: BacklogItem,
    residuals: Residuals,
    workdir: Path,
    *,
    reviewer_feedback: str = "",
    attempt: int = 0,
) -> str:
    # On the first attempt, ship the original source. On retries (attempt > 0),
    # only the freshly-generated Azure Function target files need to be shown,
    # because the reviewer already assessed those. Re-sending originals on
    # every retry was doubling the conversation size.
    if attempt == 0:
        source_blocks = _render_source_files(workdir, item, show_generated=False)
        source_heading = "## Original source (AWS Lambda — you will REPLACE this entire tree)"
    else:
        source_blocks = _render_generated_only(workdir, item)
        source_heading = "## Your previous attempt (revise these — do NOT re-emit the AWS originals)"

    # Enumerate the source filenames so the translator knows what to replace.
    src_names = [Path(p).name for p in item.source_paths]

    contract = item.contract.model_dump_json(indent=2)
    feedback_section = ""
    if reviewer_feedback:
        feedback_section = (
            "\n## Reviewer feedback on the previous attempt (address these)\n"
            f"{reviewer_feedback}\n"
        )
    return (
        "Migrate this AWS Lambda to a Python Azure Function (v2 programming model).\n\n"
        f"## Unit\n- id: `{item.unit_id}`\n- complexity: {item.complexity}\n- wave: {item.wave}\n"
        f"- attempt: {attempt}\n\n"
        f"## Contract\n```json\n{contract}\n```\n\n"
        f"## Residuals (what deterministic recipes could not fix)\n{residuals.to_markdown(workdir)}\n"
        f"{feedback_section}\n"
        f"{source_heading}\n{source_blocks}\n\n"
        "### Output protocol\n"
        "Emit each output file as a fenced block with a path header, e.g.:\n\n"
        "```python path=function_app.py\n<file contents>\n```\n\n"
        "## REQUIRED deliverables\n"
        "Every migrated unit MUST include:\n"
        "1. `function_app.py` — Azure Functions v2 entry, ONE handler with the correct trigger\n"
        "   decorator (http/service_bus/event_grid/timer) inferred from the original Lambda.\n"
        "2. `requirements.txt` — pin `azure-functions>=1.21` plus every Azure SDK package used\n"
        "   (`azure-identity`, `azure-cosmos`, `azure-servicebus`, `azure-storage-blob`,\n"
        "   `azure-eventgrid`, etc.). NEVER include `boto3` or `botocore`.\n"
        "3. `local.settings.json.example` — all app-settings env vars referenced by the code.\n"
        "4. `README.md` — how to deploy & run locally.\n\n"
        "## Services: you MUST migrate every shared-library file, not just `function_app.py`.\n"
        "For EVERY source file listed above, emit a migrated replacement using Azure SDK ONLY.\n"
        f"Input files to replace: {', '.join(f'`{n}`' for n in src_names)}\n"
        "Rules:\n"
        "- Replace `boto3.client('sqs')` → Service Bus via `azure.servicebus.ServiceBusClient`.\n"
        "- Replace `boto3.resource('dynamodb')` → `azure.cosmos.CosmosClient`.\n"
        "- Replace `boto3.client('s3')` → `azure.storage.blob.BlobServiceClient`.\n"
        "- Replace `boto3.client('eventbridge')` / SNS → `azure.eventgrid.EventGridPublisherClient`.\n"
        "- Replace `boto3.client('secretsmanager')` → `azure.keyvault.secrets.SecretClient`.\n"
        "- Use `azure.identity.DefaultAzureCredential()` — NEVER hard-coded keys.\n"
        "- Relative Python imports (`from ..services import X`) MUST keep working. Rewrite them\n"
        "  as sibling imports inside the function app package (e.g. `from services import X`\n"
        "  with a `services/__init__.py`).\n"
        "- DO NOT keep any `boto3` / `botocore` / `aws-sdk` / `amazonaws` import in ANY file.\n"
        "If a file has no useful content after migration (e.g. it only held boto3 clients that\n"
        "no longer exist in Azure), delete it by emitting a fenced block with a single comment\n"
        "line: `# (intentionally empty — functionality moved to function_app.py)`."
    )


def _render_generated_only(workdir: Path, item: BacklogItem) -> str:
    """Render only the Azure target files (not the seeded AWS originals)."""
    src_names = {Path(p).name for p in item.source_paths}
    candidates = [p for p in workdir.glob("*") if p.is_file() and p.name not in src_names]
    if not candidates:
        return "(no prior output — first attempt or translator emitted nothing)"

    out: list[str] = []
    rendered_bytes = 0
    for p in sorted(candidates, key=lambda p: p.stat().st_size):
        size = p.stat().st_size
        if size > _MAX_INLINE_FILE_BYTES:
            out.append(f"### `{p.name}` (elided: {size} bytes)")
            continue
        if rendered_bytes + size > _MAX_TOTAL_SOURCE_BYTES:
            out.append(f"### `{p.name}` (elided: budget exhausted)")
            continue
        lang = _lang_for_suffix(p.suffix)
        out.append(
            f"### `{p.name}`\n```{lang}\n"
            f"{p.read_text(encoding='utf-8', errors='replace')}\n```"
        )
        rendered_bytes += size
    return "\n\n".join(out)


def _parse_reviewer_verdict(response_text: str) -> tuple[str, str]:
    """Extract reviewer verdict + human-readable findings summary.

    Handles responses wrapped in ```json fences and nested braces inside
    ``findings``.
    """
    doc = _extract_first_json_object(response_text)
    if doc is None or not isinstance(doc, dict):
        return "unknown", response_text[:2000]
    verdict = str(doc.get("verdict", "unknown")).lower()
    findings = doc.get("findings", []) if isinstance(doc, dict) else []
    lines = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = f.get("severity", "info")
        msg = f.get("message", "")
        file = f.get("file", "?")
        ln = f.get("line", "?")
        lines.append(f"- [{sev}] `{file}:{ln}` {msg}")
    suggested_fix = doc.get("suggested_fix", "")
    feedback = "\n".join(lines)
    if suggested_fix:
        feedback += f"\n\n**Suggested fix:** {suggested_fix}"
    return verdict, feedback


def _build_reviewer_message(item: BacklogItem, workdir: Path) -> str:
    return (
        f"Review the migrated module for unit `{item.unit_id}`.\n\n"
        f"## Files under workdir (recursive)\n"
        f"{_render_source_files(workdir, item, show_generated=True)}\n\n"
        "Respond with the JSON object specified in your instructions."
    )


def _build_security_message(item: BacklogItem, workdir: Path) -> str:
    return (
        f"Scan the migrated unit `{item.unit_id}` for security issues.\n\n"
        f"{_render_source_files(workdir, item, show_generated=True)}\n\n"
        "Respond with the JSON object specified in your instructions."
    )


# Per-file and total source budgets for prompts. Tuned to keep the translator
# + reviewer + security prompts comfortably inside a 128K-token context once
# system prompt, tool definitions, and the model's output reservation are
# added. Raise carefully — if you bust context, agents fail with 400.
_MAX_INLINE_FILE_BYTES = 15_000
_MAX_TOTAL_SOURCE_BYTES = 40_000


def _render_source_files(workdir: Path, item: BacklogItem, *, show_generated: bool = False) -> str:
    """Render source files for inlining into an agent prompt.

    Elides any single file over ``_MAX_INLINE_FILE_BYTES`` and stops rendering
    once cumulative rendered size crosses ``_MAX_TOTAL_SOURCE_BYTES``; remaining
    files are listed as names only. Smallest-first ordering maximizes how many
    files fit under the cap.
    """
    candidates: list[Path]
    if show_generated:
        # Recursive so nested Maven / gradle / multi-package trees are
        # visible to the reviewer. A non-recursive glob was an
        # AWS-Lambda-era holdover that rendered the reviewer blind to
        # everything under src/main/java/... and triggered phantom
        # "Java source files missing" rejections.
        skip_dirs = {"target", "build", "node_modules", ".venv", "__pycache__"}
        candidates = [
            p for p in workdir.rglob("*")
            if p.is_file() and not any(part in skip_dirs for part in p.relative_to(workdir).parts)
        ]
    else:
        candidates = [workdir / Path(p).name for p in item.source_paths]
        candidates = [p for p in candidates if p.is_file()]

    # Smallest first so we don't blow the budget on one giant file while tiny
    # files still fit.
    candidates.sort(key=lambda p: p.stat().st_size)

    out: list[str] = []
    rendered_bytes = 0
    elided: list[str] = []

    for p in candidates:
        # Use the path relative to the workdir so reviewers see
        # ``src/main/java/com/example/app/Service.java`` rather than
        # bare ``Service.java`` (ambiguous across nested packages).
        try:
            rel = p.relative_to(workdir)
        except ValueError:
            rel = Path(p.name)
        label = str(rel)
        size = p.stat().st_size
        if size > _MAX_INLINE_FILE_BYTES:
            elided.append(f"{label} ({size} bytes)")
            continue
        if rendered_bytes + size > _MAX_TOTAL_SOURCE_BYTES:
            elided.append(f"{label} ({size} bytes)")
            continue
        lang = _lang_for_suffix(p.suffix)
        out.append(
            f"### `{label}`\n```{lang}\n"
            f"{p.read_text(encoding='utf-8', errors='replace')}\n```"
        )
        rendered_bytes += size

    if elided:
        out.append(
            "### Files elided from this prompt (too large; migrate using patterns, "
            "not literal contents)\n- " + "\n- ".join(elided)
        )
    return "\n\n".join(out)


def _lang_for_suffix(suffix: str) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".ts": "typescript",
        ".java": "java",
        ".cs": "csharp",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
    }.get(suffix.lower(), "")


def _detect_item_language(item: BacklogItem, repo_root: Path) -> str:
    for rel in item.source_paths:
        suf = Path(rel).suffix.lower()
        if suf in {".py"}:
            return "python"
        if suf in {".js", ".mjs", ".cjs"}:
            return "node"
        if suf in {".ts", ".tsx"}:
            return "typescript"
        if suf == ".java":
            return "java"
        if suf == ".cs":
            return "csharp"
    return "python"


# --------------------------------------------------------------------------- #
# Output parsers
# --------------------------------------------------------------------------- #


#: Finds every fenced code block. ``header`` captures whatever is on the
#: opener line (language tag, inline ``path=X``, info string, etc.) so the
#: inline form ``` ```xml path=pom.xml ``` works AND the bare-fence form
#: ``` ```\\n... ``` works. ``body`` is everything between the opener's
#: newline and the closing fence.
_FENCE_BLOCK_RX = re.compile(
    r"```(?P<header>[^\n]*)\n(?P<body>.*?)```",
    re.DOTALL,
)

#: The five path-header conventions we've seen LLMs emit. All are tried per
#: fenced block; first match wins. Without this, INTCALC output once landed
#: three full translations in ``translator_response.md`` because the LLM
#: picked ``<!-- path=X -->`` instead of the inline ``path=X`` form.
_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*path=(?P<path>\S+)\s*$", re.MULTILINE),          # bare
    re.compile(r"<!--\s*path=(?P<path>\S+?)\s*-->"),                  # HTML comment
    re.compile(r"//\s*path=(?P<path>\S+)"),                           # //
    re.compile(r"#\s*path=(?P<path>\S+)"),                            # #
    re.compile(r"/\*\s*path=(?P<path>\S+?)\s*\*/"),                   # /* */
)
_INLINE_PATH_RX = re.compile(r"path=(?P<path>\S+)")
# Kept for backward-compat with any code / tests that import the old name.
_FENCE_WITH_PATH = _FENCE_BLOCK_RX


def _apply_translator_output(response_text: str, workdir: Path, result: UnitResult) -> None:
    """Extract fenced file blocks from the agent's response and write them.

    Accepts five path-header conventions (any of these marks the fenced
    block as a file the translator wants written to ``path``):

    1. ``` ```lang path=rel/path ``` (inline on the fence opener)
    2. ``<!-- path=rel/path -->`` (HTML comment on the first body line)
    3. ``// path=rel/path`` (Java / JS / C++ line comment)
    4. ``# path=rel/path`` (Python / YAML / shell line comment)
    5. ``/* path=rel/path */`` (C-style block comment)

    Being tolerant here is load-bearing: LLMs are inconsistent about
    convention choice, and a single run's output routinely mixes forms.
    Each missed block used to silently become prose in
    ``translator_response.md`` with no Java files written.
    """
    workdir_resolved = workdir.resolve()
    written: list[str] = []

    for match in _FENCE_BLOCK_RX.finditer(response_text):
        header = match.group("header") or ""
        body_raw = match.group("body") or ""
        # Inline ``path=`` on the fence-opener line wins (most common).
        # Otherwise scan the body for a comment-style path header.
        rel: str | None = None
        inline = _INLINE_PATH_RX.search(header)
        if inline:
            rel = inline.group("path").strip()
        else:
            rel = _extract_path_from_text(body_raw)
        if rel is None:
            continue
        # Strip any path-header comment line out of the body so the
        # file we write to disk doesn't contain its own
        # ``<!-- path=... -->`` / ``// path=...`` marker.
        body = _strip_path_header_lines(body_raw)

        dest = (workdir / rel).resolve()
        try:
            rel_display = dest.relative_to(workdir_resolved)
        except ValueError:
            logger.warning("refusing to write outside workdir: %s", dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written.append(str(rel_display))

    if not written:
        logger.info(
            "translator emitted no fenced file blocks; raw response saved to translator_response.md"
        )
        (workdir / "translator_response.md").write_text(response_text, encoding="utf-8")


def _extract_path_from_text(text: str) -> str | None:
    """Try every known path-header convention; return first match."""
    for pattern in _PATH_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group("path").strip()
    return None


def _strip_path_header_lines(body: str) -> str:
    """Remove lines that are nothing but a path-header comment.

    Keeps the file contents clean — a translator writing
    ``<!-- path=pom.xml -->`` on line 1 shouldn't end up with that
    marker inside the emitted pom.xml. Lines with other content next
    to a path header are left intact (judgment-call; almost never
    happens in practice).
    """
    cleaned: list[str] = []
    for line in body.splitlines(keepends=True):
        stripped = line.strip()
        is_path_only_line = any(
            pattern.fullmatch(stripped) for pattern in _PATH_PATTERNS
        )
        if is_path_only_line:
            continue
        # Also drop lines that are bare ``path=...`` with nothing else.
        if stripped.startswith("path=") and " " not in stripped[5:]:
            continue
        cleaned.append(line)
    return "".join(cleaned)


def _parse_security_findings(response_text: str) -> list[str]:
    doc = _extract_first_json_object(response_text)
    if not isinstance(doc, dict):
        return []
    findings = doc.get("findings", [])
    return [
        f"[{f.get('severity','?')}] {f.get('file','?')}:{f.get('line','?')} {f.get('message','?')}"
        for f in findings
        if isinstance(f, dict)
    ]


def _extract_first_json_object(text: str) -> dict | None:
    """Find the first balanced ``{...}`` JSON object in ``text``, handling nesting.

    Skips ```json fences and walks character-by-character counting braces so
    nested arrays/objects don't break the extraction.
    """
    # Strip common fence wrappers.
    for fence in ("```json", "```"):
        if fence in text:
            text = text.split(fence, 1)[1]
            break

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
