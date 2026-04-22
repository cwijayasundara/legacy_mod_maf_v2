"""Tests for the JCL ingester.

JCL is the mainframe's IaC. Its job/step/DD topology is what the planner
needs to wave-order multi-program batch estates. This test exercises the
full path from ``.jcl`` file on disk to KG nodes/edges.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.kg_extractors.jcl import (
    ingest_jcl,
)
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "corpus"
    / "fixtures"
    / "payroll"
)


@pytest.fixture()
def store_with_jcl() -> NetworkXStore:
    """KG that already has the PAYROLL program node (simulating adapter
    output), then JCL layered on top. This is how the real pipeline
    composes adapter + JCL output.
    """
    s = NetworkXStore()
    s.add_node(
        KGNode(
            id="program:PAYROLL",
            kind="program",
            name="PAYROLL",
            span=SourceSpan(file="PAYROLL.cbl", start_line=1, end_line=53),
        )
    )
    CARTRIDGE.ingest_kg_extras(FIXTURE_ROOT, s)
    return s


def test_job_and_steps_detected(store_with_jcl: NetworkXStore):
    jobs = {n.name for n in store_with_jcl.iter_nodes(kind="job")}
    assert jobs == {"PAYROLLJ"}
    steps = {n.name for n in store_with_jcl.iter_nodes(kind="step")}
    assert steps == {"STEP010", "STEP020"}


def test_step_carries_program_name_in_attributes(store_with_jcl: NetworkXStore):
    step = store_with_jcl.get_node("step:PAYROLLJ:STEP010")
    assert step is not None
    assert step.attributes.get("pgm") == "PAYROLL"


def test_job_contains_its_steps(store_with_jcl: NetworkXStore):
    step_names = {
        n.name
        for n in store_with_jcl.neighbors(
            "job:PAYROLLJ", direction="out", edge_kinds=["contains"]
        )
    }
    assert step_names == {"STEP010", "STEP020"}


def test_step_of_edge_wires_program_to_step(store_with_jcl: NetworkXStore):
    """This is the load-bearing edge for Phase 2's seam-aware planner:
    given a program, we need to know which JCL steps invoke it.
    """
    edges = list(
        store_with_jcl.iter_edges(source="program:PAYROLL", kind="step_of")
    )
    assert any(e.target == "step:PAYROLLJ:STEP010" for e in edges)


def test_dd_of_edges_create_dataset_nodes(store_with_jcl: NetworkXStore):
    datasets = {n.name for n in store_with_jcl.iter_nodes(kind="dataset_ref")}
    # STEPLIB/PAYIN/PAYOUT/SYSOUT/AUDIN/AUDOUT, but PAYOUT==AUDIN (daily file)
    # so the dedup collapses them.
    assert "PROD.LOADLIB" in datasets
    assert "PROD.PAYROLL.MASTER" in datasets
    assert "PROD.PAYROLL.DAILY" in datasets          # shared between STEP010 output + STEP020 input
    assert "PROD.AUDIT.LOG" in datasets


def test_dd_edges_carry_dd_name_attribute(store_with_jcl: NetworkXStore):
    """DD logical names (PAYIN, PAYOUT) are the bridge between JCL and the
    COBOL ``SELECT ... ASSIGN TO DDPAYIN`` clauses. Must survive onto the
    edge attributes so later stages can correlate.
    """
    dd_edges = [
        e for e in store_with_jcl.iter_edges(kind="dd_of")
        if e.source == "step:PAYROLLJ:STEP010"
    ]
    dd_names = {e.attributes.get("dd_name") for e in dd_edges}
    assert {"STEPLIB", "PAYIN", "PAYOUT", "SYSOUT"} <= dd_names


def test_shared_dataset_links_pipeline_steps(store_with_jcl: NetworkXStore):
    """STEP010 writes PROD.PAYROLL.DAILY, STEP020 reads the same dataset.
    Both steps must emit ``dd_of`` edges to the same dataset node — that's
    how the planner infers cross-step dependencies.
    """
    daily_ds = "dataset_ref:PROD.PAYROLL.DAILY"
    producing = list(store_with_jcl.iter_edges(target=daily_ds, kind="dd_of"))
    sources = {e.source for e in producing}
    assert "step:PAYROLLJ:STEP010" in sources
    assert "step:PAYROLLJ:STEP020" in sources


def test_continuation_lines_joined(tmp_path: Path):
    """JCL lets you split a card across lines with leading blanks after
    ``//``. Make sure we reconstruct the logical card before matching.
    """
    jcl_path = tmp_path / "SAMPLE.jcl"
    jcl_path.write_text(
        "//SAMPLE JOB\n"
        "//S1 EXEC PGM=FOO\n"
        "//BIGDD DD DSN=PROD.BIG.FILE,\n"
        "//           DISP=(NEW,CATLG,DELETE)\n"
    )
    s = NetworkXStore()
    ingest_jcl(tmp_path, s)
    # Dataset must resolve despite the continuation line.
    assert s.has_node("dataset_ref:PROD.BIG.FILE")


def test_comments_ignored(tmp_path: Path):
    jcl_path = tmp_path / "WITHCOMMENT.jcl"
    jcl_path.write_text(
        "//JOBX JOB\n"
        "//* this is a JCL comment — must not become a step\n"
        "//S1 EXEC PGM=REALPGM\n"
        "//IN DD DSN=X.Y\n"
    )
    s = NetworkXStore()
    ingest_jcl(tmp_path, s)
    steps = list(s.iter_nodes(kind="step"))
    assert len(steps) == 1
    assert steps[0].name == "S1"
