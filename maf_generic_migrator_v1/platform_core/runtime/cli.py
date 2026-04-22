"""Command-line entry point: ``legacy-mod``.

Commands:
  ``cartridges``  list cartridges available on disk
  ``inspect``     show a cartridge's source/target ecosystems + agent list
  ``plan``        run discovery + graph + planner (no LLM, no writes)
  ``migrate``     run the full pipeline end-to-end
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from maf_generic_migrator_v1.platform_core.cartridge import list_cartridges, load_cartridge_by_id
from maf_generic_migrator_v1.platform_core.pipeline.planner import dump_graph_dot, dump_graph_mermaid

from .env_loader import load_env

load_env()

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.command()
def cartridges() -> None:
    """List cartridges available on disk."""
    table = Table(title="Cartridges")
    table.add_column("id")
    table.add_column("source")
    table.add_column("target")
    table.add_column("version")

    for cid in list_cartridges():
        try:
            c = load_cartridge_by_id(cid)
        except Exception as exc:  # noqa: BLE001
            table.add_row(cid, "[red]error[/red]", str(exc), "-")
            continue
        table.add_row(cid, c.source.short_id(), c.target.short_id(), c.version)
    console.print(table)


@app.command()
def inspect(cartridge: str) -> None:
    c = load_cartridge_by_id(cartridge)
    console.rule(f"[bold]{c.id}[/bold] — {c.description}")
    console.print(f"source: {c.source.model_dump()}")
    console.print(f"target: {c.target.model_dump()}")
    console.print(f"adapters: {list(c.adapters().keys())}")
    console.print(f"translators: {[a.name for a in c.translator_agents()]}")
    console.print(f"recipes: {len(c.recipes())}")
    console.print(f"service mappings: {len(c.service_map())}")
    console.print(f"rubrics: {[r.id for r in c.rubrics()]}")


@app.command()
def plan(
    cartridge: str = typer.Option(..., "--cartridge", "-c"),
    source: Path = typer.Option(..., "--source", "-s", exists=True, file_okay=False),
    out: Path = typer.Option(Path(".workspace"), "--out", "-o"),
) -> None:
    """Dry-run discovery + graph + planner; writes graph.dot and backlog.json."""

    _init_logging()
    c = load_cartridge_by_id(cartridge)
    from maf_generic_migrator_v1.platform_core.pipeline.discovery import discover
    from maf_generic_migrator_v1.platform_core.pipeline.grapher import build_graph
    from maf_generic_migrator_v1.platform_core.pipeline.planner import plan_migration

    console.print(f"[bold]discovery[/bold]: scanning {source}")
    inventory = discover(source, c)
    console.print(f"  units: {len(inventory.units)} (warnings: {len(inventory.scan_warnings)})")
    for w in inventory.scan_warnings[:10]:
        console.print(f"    {w}")

    console.print("[bold]graph[/bold]: building cross-unit dependency graph")
    graph = build_graph(inventory, c)
    console.print(f"  edges: {len(graph.edges)}  ambiguous: {len(graph.ambiguous_edges)}")

    console.print("[bold]planner[/bold]: computing migration waves")
    backlog = plan_migration(inventory, graph, c)
    console.print(f"  waves: {len(backlog.waves)}")
    for i, wave in enumerate(backlog.waves, 1):
        console.print(f"    wave {i}: {[item.unit_id for item in wave]}")

    out.mkdir(parents=True, exist_ok=True)
    (out / "inventory.json").write_text(inventory.model_dump_json(indent=2))
    (out / "graph.json").write_text(graph.model_dump_json(indent=2))
    (out / "backlog.json").write_text(backlog.model_dump_json(indent=2))
    dump_graph_dot(graph, out / "graph.dot")
    dump_graph_mermaid(graph, out / "graph.mmd")

    console.print(f"[green]plan complete[/green] — artifacts in {out}")


@app.command()
def migrate(
    cartridge: str = typer.Option(..., "--cartridge", "-c"),
    source: Path = typer.Option(..., "--source", "-s", exists=True, file_okay=False),
    out: Path = typer.Option(Path(".workspace"), "--out", "-o"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    max_budget_usd: float = typer.Option(50.0, "--max-budget-usd"),
    max_parallel: int = typer.Option(4, "--max-parallel"),
) -> None:
    """Run the full migration pipeline."""
    _init_logging()
    c = load_cartridge_by_id(cartridge)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = out / "checkpoints"

    from ..maf_workflows.main_workflow import WorkflowConfig, run_sync

    cfg = WorkflowConfig(
        repo_root=source.resolve(),
        workdir=out.resolve(),
        checkpoint_dir=checkpoint_dir.resolve(),
        max_budget_usd=max_budget_usd,
        max_parallel_units=max_parallel,
        dry_run=dry_run,
    )
    checkpoint = run_sync(c, cfg)
    console.print(f"[green]run {checkpoint.run_id} -> stage={checkpoint.stage}[/green]")
    console.print(f"  waves completed: {len(checkpoint.waves)}")


def _init_logging() -> None:
    level = os.getenv("LEGACY_MOD_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")


if __name__ == "__main__":
    app()
