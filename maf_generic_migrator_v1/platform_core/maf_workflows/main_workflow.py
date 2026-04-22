"""Top-level MAF workflow: discovery -> graph -> plan -> per-wave fanout -> verify -> aggregate.

Two entry points:
  * ``run_sync(cartridge, cfg)`` — pure-Python reference executor for tests
    and environments without MAF credentials.
  * ``run_async(cartridge, cfg)`` — durable MAF workflow execution with
    ``FileCheckpointStorage``. Both return a ``MigrationCheckpoint``.

Note: intentionally avoids ``from __future__ import annotations`` because
MAF's executor decorator introspects runtime annotations on the ``ctx``
parameter. With string annotations, MAF's validation fails.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..cartridge import MigrationCartridge
from ..ir import BacklogItem, DependencyGraph, Inventory, PlatformBacklog
from ..pipeline.discovery import discover
from ..pipeline.grapher import build_graph
from ..pipeline.planner import plan_migration
from .budget import BudgetTracker, set_current as set_budget
from .checkpoints import MigrationCheckpoint, UnitResult, WaveResult

if TYPE_CHECKING:
    from .agent_factory import AgentBundle

logger = logging.getLogger(__name__)


@dataclass
class WorkflowConfig:
    repo_root: Path
    workdir: Path
    checkpoint_dir: Path
    max_budget_usd: float = 50.0
    max_parallel_units: int = 4
    human_in_loop: bool = False
    dry_run: bool = False
    skip_llm: bool = False


# --------------------------------------------------------------------------- #
# Stage functions (pure, no MAF dependency)
# --------------------------------------------------------------------------- #


def run_discovery_stage(cartridge: MigrationCartridge, cfg: WorkflowConfig) -> Inventory:
    logger.info("discovery: scanning %s", cfg.repo_root)
    inventory = discover(cfg.repo_root, cartridge)
    logger.info("discovery: %d units (%d warnings)", len(inventory.units), len(inventory.scan_warnings))
    return inventory


def run_graph_stage(inventory: Inventory, cartridge: MigrationCartridge) -> DependencyGraph:
    logger.info("grapher: building cross-unit graph")
    graph = build_graph(inventory, cartridge)
    logger.info("grapher: %d edges (%d ambiguous)", len(graph.edges), len(graph.ambiguous_edges))
    return graph


def run_planner_stage(
    inventory: Inventory, graph: DependencyGraph, cartridge: MigrationCartridge
) -> PlatformBacklog:
    logger.info("planner: computing waves")
    backlog = plan_migration(inventory, graph, cartridge)
    logger.info("planner: %d waves (%d total units)", len(backlog.waves), sum(len(w) for w in backlog.waves))
    return backlog


def _materialize_agents_maybe(cartridge: MigrationCartridge, cfg: WorkflowConfig) -> dict:
    if cfg.skip_llm:
        return {}
    from .agent_factory import materialize_agents_lazy

    return materialize_agents_lazy(cartridge)


# --------------------------------------------------------------------------- #
# Sync reference executor
# --------------------------------------------------------------------------- #


def run_sync(cartridge: MigrationCartridge, cfg: WorkflowConfig) -> MigrationCheckpoint:
    """Run the full pipeline synchronously."""
    return asyncio.run(_run_impl(cartridge, cfg))


async def run_async(cartridge: MigrationCartridge, cfg: WorkflowConfig) -> MigrationCheckpoint:
    """Async entry; useful from MAF executors or async CLIs."""
    return await _run_impl(cartridge, cfg)


async def _run_impl(cartridge: MigrationCartridge, cfg: WorkflowConfig) -> MigrationCheckpoint:
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc)
    ckpt = MigrationCheckpoint(
        run_id=run_id,
        cartridge_id=cartridge.id,
        repo_root=str(cfg.repo_root),
        workdir=str(cfg.workdir),
        stage="discovery",
        started_at=started,
        updated_at=started,
    )
    cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = cfg.checkpoint_dir / f"{run_id}.json"

    def save() -> None:
        ckpt.updated_at = datetime.now(timezone.utc)
        ckpt.save(ckpt_path)

    ckpt.inventory = run_discovery_stage(cartridge, cfg)
    ckpt.stage = "graph"; save()

    ckpt.graph = run_graph_stage(ckpt.inventory, cartridge)
    ckpt.stage = "plan"; save()

    ckpt.backlog = run_planner_stage(ckpt.inventory, ckpt.graph, cartridge)
    ckpt.stage = "wave"; save()

    if cfg.dry_run:
        ckpt.stage = "done"; save()
        return ckpt

    agents = _materialize_agents_maybe(cartridge, cfg)
    if agents:
        logger.info("materialized %d agents: %s", len(agents), sorted(agents))

    from .unit_worker import run_unit_async

    # Install a BudgetTracker so every LLM call in this run counts against
    # ``cfg.max_budget_usd``. Once exhausted, unit_worker skips remaining
    # LLM calls and marks units as failed.
    budget = BudgetTracker(max_usd=cfg.max_budget_usd)
    set_budget(budget)
    logger.info("budget: cap=$%.2f", budget.max_usd)

    try:
        for items in ckpt.backlog.waves:
            wave_no = items[0].wave if items else 0
            wave_result = WaveResult(wave=wave_no, started_at=datetime.now(timezone.utc), items=items)
            sem = asyncio.Semaphore(max(1, cfg.max_parallel_units))

            async def _process(item: BacklogItem) -> UnitResult:
                async with sem:
                    return await run_unit_async(item, cartridge, cfg, agents)

            wave_result.unit_results = await asyncio.gather(*[_process(i) for i in items])
            wave_result.verification_passed = cartridge.verify_wave(cfg.workdir, ckpt.graph, wave_no)
            wave_result.finished_at = datetime.now(timezone.utc)
            ckpt.waves.append(wave_result)
            save()

            logger.info(budget.summary())

            if not wave_result.verification_passed:
                logger.error("wave %d failed verification — halting", wave_no)
                break
            if budget.should_halt:
                logger.error("wave %d completed but budget exhausted — skipping remaining waves", wave_no)
                break
    finally:
        set_budget(None)

    logger.info("final %s", budget.summary())
    _persist_cost_report(cfg, ckpt, budget)
    ckpt.stage = "done"; save()
    return ckpt


def _persist_cost_report(cfg: "WorkflowConfig", ckpt: MigrationCheckpoint, budget: "BudgetTracker") -> None:
    """Write two cost artifacts per run:

    * ``{workdir}/cost_reports/{run_id}.json`` — full structured report with
      by-model / by-role breakdown and per-unit cost.
    * ``{workdir}/cost_log.jsonl`` — append-only one-line summary per run,
      so the history of runs for this repo_id is greppable.
    """
    import json

    unit_cost = [
        {
            "unit_id": u.unit_id,
            "wave": u.wave,
            "status": u.status,
            "cost_usd": round(u.cost_usd, 6),
            "tokens": dict(u.tokens),
        }
        for wave in ckpt.waves
        for u in wave.unit_results
    ]

    report = {
        "run_id": ckpt.run_id,
        "cartridge_id": ckpt.cartridge_id,
        "repo_root": ckpt.repo_root,
        "workdir": ckpt.workdir,
        "started_at": ckpt.started_at.isoformat(),
        "finished_at": ckpt.updated_at.isoformat(),
        "stage": ckpt.stage,
        "budget": budget.to_report(),
        "units": unit_cost,
    }

    reports_dir = Path(cfg.workdir) / "cost_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{ckpt.run_id}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    log_line = {
        "run_id": ckpt.run_id,
        "finished_at": ckpt.updated_at.isoformat(),
        "cartridge_id": ckpt.cartridge_id,
        "units": len(unit_cost),
        "calls": budget.call_count,
        "input_tokens": budget.input_tokens,
        "output_tokens": budget.output_tokens,
        "used_usd": round(budget.used_usd, 6),
        "max_usd": round(budget.max_usd, 2),
        "halted": budget.halted,
    }
    log_path = Path(cfg.workdir) / "cost_log.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(log_line) + "\n")

    logger.info("cost report written: %s  (history: %s)", report_path, log_path)


# --------------------------------------------------------------------------- #
# MAF Workflow build (for durable multi-host execution)
# --------------------------------------------------------------------------- #


def build_workflow(cartridge: MigrationCartridge, cfg: WorkflowConfig):  # noqa: ANN201 — returns agent_framework.Workflow
    """Build a durable MAF ``Workflow`` that mirrors ``_run_impl``.

    Each stage is a ``FunctionExecutor`` passing typed state. Checkpoints go
    to ``cfg.checkpoint_dir`` via ``FileCheckpointStorage``.
    """
    from agent_framework import (
        FileCheckpointStorage,
        WorkflowBuilder,
        WorkflowContext,
        executor,
    )

    cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    storage = FileCheckpointStorage(str(cfg.checkpoint_dir))

    # NOTE: MAF's runtime type-introspection needs non-string annotations on
    # the ``ctx`` parameter, so we use plain ``WorkflowContext`` and declare
    # the concrete input/output types via the decorator's keyword args.

    @executor(id="discovery", input=str, output=Inventory)
    async def discovery_stage(_trigger, ctx: WorkflowContext) -> None:
        await ctx.send_message(run_discovery_stage(cartridge, cfg))

    @executor(id="graph", input=Inventory, output=DependencyGraph)
    async def graph_stage(inv, ctx: WorkflowContext) -> None:
        await ctx.send_message(run_graph_stage(inv, cartridge))

    @executor(id="plan", input=DependencyGraph, output=PlatformBacklog)
    async def plan_stage(graph, ctx: WorkflowContext) -> None:
        inv = run_discovery_stage(cartridge, cfg)
        await ctx.send_message(run_planner_stage(inv, graph, cartridge))

    @executor(id="migrate", input=PlatformBacklog, workflow_output=MigrationCheckpoint)
    async def migrate_stage(_backlog, ctx: WorkflowContext) -> None:
        ckpt = await _run_impl(cartridge, cfg)
        await ctx.yield_output(ckpt)

    builder = WorkflowBuilder(
        start_executor=discovery_stage,
        checkpoint_storage=storage,
        max_iterations=100,
        name=f"legacy-mod-{cartridge.id}",
    )
    builder.add_edge(discovery_stage, graph_stage)
    builder.add_edge(graph_stage, plan_stage)
    builder.add_edge(plan_stage, migrate_stage)
    return builder.build()
