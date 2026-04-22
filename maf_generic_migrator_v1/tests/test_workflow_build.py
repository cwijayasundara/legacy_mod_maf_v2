"""Workflow build smoke test — verifies build_workflow() returns a real MAF
Workflow object; does not execute it (that requires LLM creds)."""
from __future__ import annotations

from pathlib import Path


def test_build_workflow_returns_maf_workflow(tmp_path: Path) -> None:
    from agent_framework import Workflow

    from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id
    from maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow import WorkflowConfig, build_workflow

    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")
    cfg = WorkflowConfig(
        repo_root=tmp_path,
        workdir=tmp_path / "wd",
        checkpoint_dir=tmp_path / "ckpt",
    )
    wf = build_workflow(cartridge, cfg)
    assert isinstance(wf, Workflow)
    assert wf.get_start_executor() is not None
    # The workflow should have at least discovery, graph, plan, migrate executors.
    executors = wf.get_executors_list()
    names = [e.id for e in executors]
    assert {"discovery", "graph", "plan", "migrate"} <= set(names)


def test_sync_run_plan_only(tmp_path: Path, tiny_polyglot_repo: Path) -> None:
    """run_sync with dry_run=True produces a full checkpoint without LLM calls."""
    from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id
    from maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow import WorkflowConfig, run_sync

    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")
    cfg = WorkflowConfig(
        repo_root=tiny_polyglot_repo,
        workdir=tmp_path / "wd",
        checkpoint_dir=tmp_path / "ckpt",
        dry_run=True,
        skip_llm=True,
    )
    ckpt = run_sync(cartridge, cfg)
    assert ckpt.stage == "done"
    assert ckpt.inventory is not None
    assert ckpt.graph is not None
    assert ckpt.backlog is not None
    assert len(ckpt.backlog.waves) >= 1
