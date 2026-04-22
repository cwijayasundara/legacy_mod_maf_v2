"""Kick off AWS Lambda -> Azure Functions migration end-to-end.

Mirrors the env-var contract and CLI shape of legacy_mod_maf_v1's
``run_migration.py``, but talks directly to the v2 in-process workflow
(``maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow.run_sync``) instead of spawning
a FastAPI orchestrator.

All knobs come from .env (loaded from the repo root):

    # --- required auth (pick one provider) ---
    OPENAI_API_KEY=sk-...
    # or AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY
    # or ANTHROPIC_API_KEY
    # or AZURE_ANTHROPIC_ENDPOINT + AZURE_ANTHROPIC_API_KEY

    # --- optional ---
    OPENAI_MODEL=gpt-5.4-mini                 # model for all agent roles
    LEGACY_MOD_PROVIDER=openai                # force a provider; otherwise autodetected
    SOURCE_DIR=aws_legacy/generated_code      # repo to migrate
    MIGRATED_DIR=azure_fn_migrated            # where artifacts are written
    REPO_ID=legacy-aws                        # logical id (used to name the workspace)
    CARTRIDGE=aws_lambda_polyglot_to_azure_fn_py
    MAX_PARALLEL=2                            # parallel unit-workers per wave
    MAX_BUDGET_USD=10.0                       # soft budget cap
    DRY_RUN=0                                 # 1 = plan-only, no LLM calls
    POLL_INTERVAL_S=15                        # progress reprint cadence

Relative paths in SOURCE_DIR / MIGRATED_DIR are resolved against the repo root.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from maf_generic_migrator_v1.platform_core.runtime.env_loader import load_env

REPO_ROOT = Path(__file__).resolve().parent

DEFAULT_SOURCE_DIR = "aws_legacy/generated_code"
DEFAULT_MIGRATED_DIR = "azure_fn_migrated"
DEFAULT_REPO_ID = "legacy-aws"
DEFAULT_CARTRIDGE = "aws_lambda_polyglot_to_azure_fn_py"


def _resolve(path_str: str) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def _bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _has_any_provider_credentials() -> bool:
    # Ollama is a valid provider that needs no credentials — only a running
    # local endpoint + a pulled model.
    if (os.getenv("LEGACY_MOD_PROVIDER", "").strip().lower() == "ollama"
            or os.getenv("OLLAMA_BASE_URL")
            or os.getenv("OLLAMA_MODEL")):
        return True
    return any(
        os.getenv(k)
        for k in (
            "OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "ANTHROPIC_API_KEY",
            "AZURE_ANTHROPIC_ENDPOINT",
            "FOUNDRY_PROJECT_ENDPOINT",
        )
    )


def _summarize_unit(unit) -> str:  # noqa: ANN001 — UnitResult, avoid hard import
    score = f" score={unit.rubric_score * 100:.0f}/100" if unit.rubric_score is not None else ""
    tests = "" if unit.tests_passed is None else f" tests={'pass' if unit.tests_passed else 'fail'}"
    verdict = f" verdict={unit.reviewer_verdict}" if unit.reviewer_verdict else ""
    cost = f" cost=${unit.cost_usd:.4f}" if unit.cost_usd else ""
    return f"{unit.unit_id}  status={unit.status}{verdict}{tests}{score}{cost}"


def main() -> int:
    load_env(REPO_ROOT / ".env")

    if not _has_any_provider_credentials() and not _bool("DRY_RUN"):
        print(
            "ERROR: no LLM credentials in .env. Set one of: "
            "OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT (+API_KEY), ANTHROPIC_API_KEY, "
            "AZURE_ANTHROPIC_ENDPOINT (+API_KEY). Or set DRY_RUN=1 for plan-only.",
            file=sys.stderr,
        )
        return 2

    source_dir = _resolve(os.getenv("SOURCE_DIR", DEFAULT_SOURCE_DIR))
    output_dir = _resolve(os.getenv("MIGRATED_DIR", DEFAULT_MIGRATED_DIR))
    repo_id = os.getenv("REPO_ID", DEFAULT_REPO_ID)
    cartridge_id = os.getenv("CARTRIDGE", DEFAULT_CARTRIDGE)
    max_parallel = int(os.getenv("MAX_PARALLEL", "2"))
    max_budget_usd = float(os.getenv("MAX_BUDGET_USD", "10.0"))
    dry_run = _bool("DRY_RUN")
    poll_interval = int(os.getenv("POLL_INTERVAL_S", "15"))

    if not source_dir.is_dir():
        print(f"ERROR: source not found: {source_dir}", file=sys.stderr)
        return 2

    workdir = output_dir / repo_id
    workdir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=os.getenv("LEGACY_MOD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Late imports so .env is in place before agent_factory reads it.
    from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id
    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import _detect_provider
    from maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow import WorkflowConfig, run_sync

    provider = _detect_provider()
    print(f"[startup] provider={provider}  cartridge={cartridge_id}  repo_id={repo_id}")
    print(f"[startup] source={source_dir}")
    print(f"[startup] target={workdir}")
    if dry_run:
        print("[startup] DRY_RUN=1 — plan-only, no LLM calls")

    cartridge = load_cartridge_by_id(cartridge_id)
    cfg = WorkflowConfig(
        repo_root=source_dir,
        workdir=workdir,
        checkpoint_dir=(workdir / "checkpoints").resolve(),
        max_parallel_units=max_parallel,
        max_budget_usd=max_budget_usd,
        dry_run=dry_run,
    )

    overall_started = time.perf_counter()

    print(f"[discover] scanning {source_dir}")
    discover_started = time.perf_counter()
    ckpt = run_sync(cartridge, cfg)
    print(f"[discover] completed in {time.perf_counter() - discover_started:.2f}s")
    if ckpt.inventory is None:
        print("ERROR: discovery returned no inventory", file=sys.stderr)
        return 3

    units = ckpt.inventory.units
    print(f"[discover] inventory has {len(units)} module(s): {[u.unit_id for u in units]}")
    if not units:
        print(
            f"ERROR: scanner produced 0 modules for {source_dir}. "
            f"Check {workdir}/checkpoints/ for the run log.",
            file=sys.stderr,
        )
        return 3

    if ckpt.backlog is None:
        print("ERROR: planner produced no backlog", file=sys.stderr)
        return 3

    backlog_total = sum(len(w) for w in ckpt.backlog.waves)
    print(f"[plan] {backlog_total} backlog items across {len(ckpt.backlog.waves)} wave(s)")
    for i, wave in enumerate(ckpt.backlog.waves, 1):
        print(f"           wave {i}: {[item.unit_id for item in wave]}")

    if dry_run:
        print(f"[timing] total run {time.perf_counter() - overall_started:.2f}s (dry-run)")
        return 0

    # `run_sync` already executed the migrate stage; replay results in v1's
    # progress style so the operator sees the same shape of output.
    print("[migrate] fanout complete; summarizing results")
    last_summary: str | None = None
    for wave_result in ckpt.waves:
        elapsed = int(time.perf_counter() - overall_started)
        hms = f"{elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}"
        counts: dict[str, int] = {}
        for u in wave_result.unit_results:
            counts[u.status] = counts.get(u.status, 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no units"
        line = f"[progress] {hms}  wave={wave_result.wave}  verify={wave_result.verification_passed}  {summary}"
        if line != last_summary:
            print(line)
            last_summary = line
        for unit in wave_result.unit_results:
            print(f"           - {_summarize_unit(unit)}")
        time.sleep(0.001 if poll_interval == 0 else 0)  # poll_interval kept for parity; results are already final

    print(f"[migrate] final stage={ckpt.stage} run_id={ckpt.run_id}")
    print(f"[timing] total run {time.perf_counter() - overall_started:.2f}s")

    from maf_generic_migrator_v1.platform_core.maf_workflows.budget import last_finalized
    budget = last_finalized()
    if budget is not None:
        status = "exhausted" if budget.should_halt else "ok"
        print(
            f"[cost] ${budget.used_usd:.4f} of ${budget.max_usd:.2f} used "
            f"across {budget.call_count} LLM calls  "
            f"({budget.input_tokens} in / {budget.output_tokens} out tok)  [{status}]"
        )
        for model, bucket in sorted(
            budget.by_model.items(), key=lambda kv: -kv[1]["usd"]
        ):
            print(
                f"         {model}: ${bucket['usd']:.4f}  "
                f"({bucket['calls']} calls, {bucket['in']}/{bucket['out']} tok)"
            )

    print(f"\nDone. All artifacts under {workdir}/:")
    print(f"  - Migrated code:    {workdir}/units/<unit_id>/")
    print(f"  - Checkpoints:      {workdir}/checkpoints/{ckpt.run_id}.json")
    print(f"  - Cost report:      {workdir}/cost_reports/{ckpt.run_id}.json")
    print(f"  - Cost history:     {workdir}/cost_log.jsonl  (one line per run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
