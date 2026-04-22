"""Tests for cross-unit CALL graph + migrated-callee API injection.

Two coupled platform features under test:

1. ``grapher_hints`` emits CALL-based cross-unit edges so the planner
   orders callees before callers. This replaces the one-wave-everyone
   behaviour with a topologically-sequenced multi-wave plan.

2. ``_collect_migrated_callee_apis`` reads the callee's already-emitted
   Java files (service + DTOs) from its unit workdir and renders them
   as a verbatim prompt section. The caller's translator then sees
   exact method signatures and DTO types instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.ir import Contract, UnitIR, Inventory
from maf_generic_migrator_v1.platform_core.pipeline.discovery import discover
from maf_generic_migrator_v1.platform_core.pipeline.grapher import build_graph
from maf_generic_migrator_v1.platform_core.pipeline.planner import plan_migration

PILOT_CORPUS = Path(__file__).resolve().parents[2] / "pilot" / "corpus"


@dataclass
class _FakeCfg:
    """Minimal WorkflowConfig stand-in for _collect_migrated_callee_apis."""

    repo_root: Path
    workdir: Path


# ---------------------------------------------------------------------- #
# Cross-unit grapher hints
# ---------------------------------------------------------------------- #


def test_grapher_hints_emits_call_edges_for_pilot_corpus():
    """STMTGEN calls INTCALC in the pilot corpus. An edge must be
    emitted so the planner sees the dependency.
    """
    # Reset the cartridge's cached KG so this test sees a fresh build.
    CARTRIDGE._RUN_KG_CACHE.clear()

    inv = discover(PILOT_CORPUS.resolve(), CARTRIDGE)
    hints = CARTRIDGE.grapher_hints(inv)

    call_edges = [(h["source_unit"], h["target_unit"]) for h in hints if h["kind"] == "calls"]
    assert ("STMTGEN", "INTCALC") in call_edges, f"hints: {hints}"


def test_planner_now_splits_stmtgen_into_later_wave():
    """With CALL hints in place, the planner must sequence INTCALC
    before STMTGEN. Previously one wave, now two.
    """
    CARTRIDGE._RUN_KG_CACHE.clear()
    inv = discover(PILOT_CORPUS.resolve(), CARTRIDGE)
    graph = build_graph(inv, CARTRIDGE)
    backlog = plan_migration(inv, graph, CARTRIDGE)

    assert len(backlog.waves) == 2
    wave1 = {item.unit_id for item in backlog.waves[0]}
    wave2 = {item.unit_id for item in backlog.waves[1]}
    # INTCALC is a leaf — no outbound calls — so it ships early.
    assert "INTCALC" in wave1
    # STMTGEN depends on INTCALC, so it's pushed out.
    assert "STMTGEN" in wave2
    assert "INTCALC" not in wave2


def test_grapher_hints_ignores_calls_to_non_inventory_targets(tmp_path: Path):
    """CALL 'EXTSYS' to something outside the inventory (external library,
    unmigrated legacy) must NOT emit an edge — we can't order against it.
    """
    source = tmp_path / "MYPROG.cbl"
    source.write_text(
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. MYPROG.\n"
        "       PROCEDURE DIVISION.\n"
        "       MAIN-SECTION SECTION.\n"
        "       MAIN.\n"
        "           CALL 'NONEXISTENT' USING WS-X.\n"
        "           GOBACK.\n"
    )
    CARTRIDGE._RUN_KG_CACHE.clear()
    inv = discover(tmp_path, CARTRIDGE)
    hints = CARTRIDGE.grapher_hints(inv)
    # NONEXISTENT isn't an inventory unit, so no edge emitted.
    assert all(h["target_unit"] != "NONEXISTENT" for h in hints)


# ---------------------------------------------------------------------- #
# Migrated-callee API injection
# ---------------------------------------------------------------------- #


def _make_fake_migrated_callee(
    tmp_path: Path, target_id: str, service_body: str, *, dto_body: str | None = None
) -> None:
    """Pre-populate a callee's workdir to simulate a completed earlier wave."""
    # Matches _java_names_for(target_id)["package_path"]
    pkg_path = f"com/example/{target_id.lower()}"
    svc_dir = tmp_path / "units" / target_id / "src/main/java" / pkg_path / "service"
    svc_dir.mkdir(parents=True, exist_ok=True)
    class_base = target_id.capitalize()
    (svc_dir / f"{class_base}Service.java").write_text(service_body, encoding="utf-8")

    if dto_body is not None:
        dto_dir = tmp_path / "units" / target_id / "src/main/java" / pkg_path / "dto"
        dto_dir.mkdir(parents=True, exist_ok=True)
        (dto_dir / f"{class_base}Result.java").write_text(dto_body, encoding="utf-8")


def test_injection_includes_service_and_dto_when_callee_migrated(tmp_path: Path):
    """Set up as if INTCALC was already translated; ensure STMTGEN's
    injection contains the service + DTO file contents verbatim.
    """
    _make_fake_migrated_callee(
        tmp_path, "INTCALC",
        service_body="package com.example.intcalc.service;\n\npublic class IntcalcService {\n    public Result calc(BigDecimal x) { return null; }\n}\n",
        dto_body="package com.example.intcalc.dto;\n\npublic record IntcalcResult(String ok, String msg) {}\n",
    )

    CARTRIDGE._RUN_KG_CACHE.clear()
    cfg = _FakeCfg(repo_root=PILOT_CORPUS.resolve(), workdir=tmp_path)
    store = CARTRIDGE._run_store(PILOT_CORPUS.resolve())
    section = CARTRIDGE._collect_migrated_callee_apis(cfg, store, "STMTGEN")

    assert "INTCALC" in section
    assert "IntcalcService" in section
    assert "IntcalcResult" in section
    assert "calc(BigDecimal x)" in section


def test_injection_empty_when_callee_not_yet_migrated(tmp_path: Path):
    """If the callee's unit workdir doesn't exist yet (e.g. same wave
    race), return empty — safe fallback for the translator.
    """
    CARTRIDGE._RUN_KG_CACHE.clear()
    cfg = _FakeCfg(repo_root=PILOT_CORPUS.resolve(), workdir=tmp_path)
    store = CARTRIDGE._run_store(PILOT_CORPUS.resolve())
    section = CARTRIDGE._collect_migrated_callee_apis(cfg, store, "STMTGEN")
    assert section == ""


def test_injection_empty_when_caller_has_no_external_calls(tmp_path: Path):
    """CUSTINQ in the pilot corpus has no CALL — nothing to inject."""
    CARTRIDGE._RUN_KG_CACHE.clear()
    cfg = _FakeCfg(repo_root=PILOT_CORPUS.resolve(), workdir=tmp_path)
    store = CARTRIDGE._run_store(PILOT_CORPUS.resolve())
    section = CARTRIDGE._collect_migrated_callee_apis(cfg, store, "CUSTINQ")
    assert section == ""


def test_injection_header_tells_llm_to_use_exact_api(tmp_path: Path):
    """The header guidance is load-bearing: without it the LLM still
    invents stub classes. Pin the wording so it doesn't silently drift.
    """
    _make_fake_migrated_callee(
        tmp_path, "INTCALC",
        service_body="public class IntcalcService { public void x() {} }\n",
    )
    CARTRIDGE._RUN_KG_CACHE.clear()
    cfg = _FakeCfg(repo_root=PILOT_CORPUS.resolve(), workdir=tmp_path)
    store = CARTRIDGE._run_store(PILOT_CORPUS.resolve())
    section = CARTRIDGE._collect_migrated_callee_apis(cfg, store, "STMTGEN")

    for marker in (
        "use verbatim",
        "inject the callee",
        "EXACT signature",
        "Do NOT re-declare a stub",
    ):
        assert marker.lower() in section.lower(), f"missing marker: {marker!r}"


def test_injection_respects_size_budget(tmp_path: Path):
    """If the callee's Java is gigantic, the injector truncates with
    an explicit ``elided`` note rather than busting the prompt context.
    """
    # 50 KB of filler — over the _MAX_CALLEE_API_BYTES=40 KB ceiling.
    from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot import cartridge as c_mod
    assert c_mod._MAX_CALLEE_API_BYTES == 40_000

    huge = "x" * 50_000
    _make_fake_migrated_callee(
        tmp_path, "INTCALC",
        service_body=f"public class IntcalcService {{\n  // {huge}\n}}\n",
        dto_body="public record IntcalcResult() {}\n",
    )
    CARTRIDGE._RUN_KG_CACHE.clear()
    cfg = _FakeCfg(repo_root=PILOT_CORPUS.resolve(), workdir=tmp_path)
    store = CARTRIDGE._run_store(PILOT_CORPUS.resolve())
    section = CARTRIDGE._collect_migrated_callee_apis(cfg, store, "STMTGEN")

    # The section is present (we got SOMETHING)...
    assert "INTCALC" in section
    # ...but flags that content was cut off.
    assert "elided" in section.lower()
