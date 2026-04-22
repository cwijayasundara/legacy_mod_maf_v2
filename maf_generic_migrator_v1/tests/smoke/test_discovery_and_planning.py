"""End-to-end smoke: discover + graph + plan a tiny polyglot repo (no LLM, no writes)."""
from __future__ import annotations

from pathlib import Path

from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id
from maf_generic_migrator_v1.platform_core.pipeline.discovery import discover
from maf_generic_migrator_v1.platform_core.pipeline.grapher import build_graph
from maf_generic_migrator_v1.platform_core.pipeline.planner import plan_migration


def test_polyglot_smoke(tiny_polyglot_repo: Path) -> None:
    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")

    inventory = discover(tiny_polyglot_repo, cartridge)
    assert len(inventory.units) >= 3
    langs = {u.language for u in inventory.units}
    # At least the three explicit languages should be discovered.
    assert {"python", "node", "java"} <= langs

    graph = build_graph(inventory, cartridge)
    # Expect the SAM-derived hints to have seeded cross-Lambda edges/resources.
    assert len(graph.resources) >= 1 or len(graph.edges) >= 1

    backlog = plan_migration(inventory, graph, cartridge)
    assert backlog.waves, "planner produced no waves"
    flat = [item for wave in backlog.waves for item in wave]
    assert len(flat) == len(inventory.units)
    for item in flat:
        assert item.cartridge_id == cartridge.id
        assert item.source_paths


def test_inventory_has_service_calls(tiny_polyglot_repo: Path) -> None:
    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")
    inventory = discover(tiny_polyglot_repo, cartridge)
    all_calls = [c for u in inventory.units for c in u.service_calls]
    kinds = {c.service for c in all_calls}
    # python unit invokes sqs + dynamodb; node unit invokes sns; java invokes s3
    assert {"sqs", "dynamodb", "sns", "s3"} <= kinds
