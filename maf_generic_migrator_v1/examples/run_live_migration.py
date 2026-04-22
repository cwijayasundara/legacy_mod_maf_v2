"""Live end-to-end migration of the polyglot demo against a real LLM.

Default provider: OpenAI with ``gpt-5.4-mini``. Other providers are picked up
from env vars (see ``maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory``).

Run::

    export OPENAI_API_KEY=sk-...
    python examples/run_live_migration.py

Or point it at your own source repo::

    python examples/run_live_migration.py --source path/to/lambdas --out path/to/out
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from maf_generic_migrator_v1.platform_core.runtime.env_loader import load_env

load_env()

DEMO_REPO = Path(__file__).resolve().parent / "demo_migrations" / "aws_polyglot"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=DEMO_REPO, help="Source repo to migrate")
    p.add_argument("--out", type=Path, default=Path(".workspace/live_run"), help="Output workdir")
    p.add_argument("--cartridge", default="aws_lambda_polyglot_to_azure_fn_py")
    p.add_argument("--dry-run", action="store_true", help="Plan only, skip LLM")
    p.add_argument("--max-parallel", type=int, default=2)
    p.add_argument("--max-budget-usd", type=float, default=10.0)
    args = p.parse_args()

    logging.basicConfig(
        level=os.getenv("LEGACY_MOD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("legacy-mod")

    from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id
    from maf_generic_migrator_v1.platform_core.maf_workflows.agent_factory import _detect_provider
    from maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow import WorkflowConfig, run_sync

    provider = _detect_provider()
    log.info("provider=%s source=%s out=%s", provider, args.source, args.out)
    if not args.dry_run and provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY is not set. Export it, choose another provider via "
            "LEGACY_MOD_PROVIDER, or add --dry-run for plan-only execution."
        )

    cartridge = load_cartridge_by_id(args.cartridge)
    cfg = WorkflowConfig(
        repo_root=args.source.resolve(),
        workdir=args.out.resolve(),
        checkpoint_dir=(args.out / "checkpoints").resolve(),
        max_parallel_units=args.max_parallel,
        max_budget_usd=args.max_budget_usd,
        dry_run=args.dry_run,
    )

    ckpt = run_sync(cartridge, cfg)
    log.info("run %s -> stage=%s", ckpt.run_id, ckpt.stage)

    # Summary
    for wave in ckpt.waves:
        log.info("wave %d verification=%s", wave.wave, wave.verification_passed)
        for unit in wave.unit_results:
            log.info(
                "  %s status=%s verdict=%s tests=%s rubric=%s",
                unit.unit_id,
                unit.status,
                unit.reviewer_verdict,
                unit.tests_passed,
                f"{unit.rubric_score:.2f}" if unit.rubric_score is not None else "-",
            )

    print(f"\nArtifacts under {args.out}:")
    for unit_dir in sorted((args.out / "units").iterdir()) if (args.out / "units").is_dir() else []:
        files = sorted(f.name for f in unit_dir.iterdir() if f.is_file())
        print(f"  {unit_dir.name}/  ->  {', '.join(files)}")


if __name__ == "__main__":
    main()
