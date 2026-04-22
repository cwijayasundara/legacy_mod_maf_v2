"""Tests for the CRUD matrix builder."""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)
from maf_generic_migrator_v1.platform_core.pipeline.crud import (
    CRUDOps,
    ResourceRef,
    build_crud_matrix,
)

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "corpus"
    / "fixtures"
    / "payroll"
)


# --------------------------------------------------------------------------- #
# Fixture construction helper
# --------------------------------------------------------------------------- #


def _span(line: int = 1) -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=line, end_line=line)


def _build_two_program_store() -> NetworkXStore:
    """Hand-built toy: one writer (LOADER), one reader (REPORTER), one
    shared dataset (CUSTOMERS). JCL wires both programs to the same DD
    name so the CRUD resolver has to work.
    """
    s = NetworkXStore()

    # Programs
    for pid in ("LOADER", "REPORTER"):
        s.add_node(KGNode(id=f"program:{pid}", kind="program", name=pid, span=_span()))
        s.add_node(
            KGNode(
                id=f"paragraph:{pid}:MAIN",
                kind="paragraph",
                name="MAIN",
                span=_span(),
            )
        )
        s.add_edge(KGEdge(source=f"program:{pid}", target=f"paragraph:{pid}:MAIN", kind="contains"))

    # FDs with DD-name bindings
    s.add_node(
        KGNode(
            id="file:LOADER:CUST-OUT",
            kind="file",
            name="CUST-OUT",
            span=_span(),
            attributes={"dd_name": "CUSTOUT"},
        )
    )
    s.add_node(
        KGNode(
            id="file:REPORTER:CUST-IN",
            kind="file",
            name="CUST-IN",
            span=_span(),
            attributes={"dd_name": "CUSTIN"},
        )
    )
    s.add_edge(KGEdge(source="program:LOADER", target="file:LOADER:CUST-OUT", kind="contains"))
    s.add_edge(KGEdge(source="program:REPORTER", target="file:REPORTER:CUST-IN", kind="contains"))

    # Paragraph-level reads/writes
    s.add_edge(
        KGEdge(
            source="paragraph:LOADER:MAIN",
            target="file:LOADER:CUST-OUT",
            kind="writes",
            evidence="LOADER.cbl:10 WRITE",
        )
    )
    s.add_edge(
        KGEdge(
            source="paragraph:REPORTER:MAIN",
            target="file:REPORTER:CUST-IN",
            kind="reads",
            evidence="REPORTER.cbl:15 READ",
        )
    )

    # JCL: both programs touch the same physical dataset via different DDs.
    s.add_node(KGNode(id="job:NIGHTLY", kind="job", name="NIGHTLY", span=_span()))
    s.add_node(KGNode(id="step:NIGHTLY:S1", kind="step", name="S1", span=_span(), attributes={"pgm": "LOADER"}))
    s.add_node(KGNode(id="step:NIGHTLY:S2", kind="step", name="S2", span=_span(), attributes={"pgm": "REPORTER"}))
    s.add_node(KGNode(id="dataset_ref:PROD.CUSTOMERS", kind="dataset_ref", name="PROD.CUSTOMERS", span=_span()))
    s.add_edge(KGEdge(source="job:NIGHTLY", target="step:NIGHTLY:S1", kind="contains"))
    s.add_edge(KGEdge(source="job:NIGHTLY", target="step:NIGHTLY:S2", kind="contains"))
    s.add_edge(KGEdge(source="program:LOADER", target="step:NIGHTLY:S1", kind="step_of"))
    s.add_edge(KGEdge(source="program:REPORTER", target="step:NIGHTLY:S2", kind="step_of"))
    s.add_edge(
        KGEdge(
            source="step:NIGHTLY:S1",
            target="dataset_ref:PROD.CUSTOMERS",
            kind="dd_of",
            attributes={"dd_name": "CUSTOUT"},
        )
    )
    s.add_edge(
        KGEdge(
            source="step:NIGHTLY:S2",
            target="dataset_ref:PROD.CUSTOMERS",
            kind="dd_of",
            attributes={"dd_name": "CUSTIN"},
        )
    )
    return s


# --------------------------------------------------------------------------- #
# Structural tests on the hand-built toy store
# --------------------------------------------------------------------------- #


def test_matrix_lists_both_programs():
    matrix = build_crud_matrix(_build_two_program_store())
    assert matrix.programs() == ["LOADER", "REPORTER"]


def test_fd_resolves_to_dataset():
    """LOADER writes CUST-OUT; CUSTOUT DD is bound to PROD.CUSTOMERS —
    the matrix must record the op on BOTH the FD and the dataset.
    """
    matrix = build_crud_matrix(_build_two_program_store())

    fd = ResourceRef(kind="file", name="CUST-OUT")
    ds = ResourceRef(kind="dataset", name="PROD.CUSTOMERS")

    assert matrix.ops("LOADER", fd) is not None
    assert matrix.ops("LOADER", fd).create is True

    assert matrix.ops("LOADER", ds) is not None
    assert matrix.ops("LOADER", ds).create is True


def test_shared_dataset_visible_to_both_programs():
    matrix = build_crud_matrix(_build_two_program_store())
    ds = ResourceRef(kind="dataset", name="PROD.CUSTOMERS")
    programs = matrix.programs_touching(ds)
    assert programs == ["LOADER", "REPORTER"]


def test_readers_and_writers_split_correctly():
    matrix = build_crud_matrix(_build_two_program_store())
    ds = ResourceRef(kind="dataset", name="PROD.CUSTOMERS")
    assert matrix.readers_of(ds) == ["REPORTER"]
    assert matrix.writers_of(ds) == ["LOADER"]


def test_touches_only_when_no_source_rw():
    """If a program has a JCL step_of → dd_of chain but no paragraph-level
    R/W evidence, the matrix records ``touches_only`` (the ``*`` symbol).
    """
    s = _build_two_program_store()
    # Third program with JCL-only evidence.
    s.add_node(KGNode(id="program:ARCHIVER", kind="program", name="ARCHIVER", span=_span()))
    s.add_node(KGNode(id="step:NIGHTLY:S3", kind="step", name="S3", span=_span(), attributes={"pgm": "ARCHIVER"}))
    s.add_edge(KGEdge(source="job:NIGHTLY", target="step:NIGHTLY:S3", kind="contains"))
    s.add_edge(KGEdge(source="program:ARCHIVER", target="step:NIGHTLY:S3", kind="step_of"))
    s.add_edge(
        KGEdge(
            source="step:NIGHTLY:S3",
            target="dataset_ref:PROD.CUSTOMERS",
            kind="dd_of",
            attributes={"dd_name": "ARCHIN"},
        )
    )

    matrix = build_crud_matrix(s)
    ds = ResourceRef(kind="dataset", name="PROD.CUSTOMERS")
    ops = matrix.ops("ARCHIVER", ds)
    assert ops is not None
    assert ops.touches_only is True
    assert ops.symbol == "*"


def test_crudops_symbol_rendering():
    ops = CRUDOps(read=True)
    assert ops.symbol == "R"
    ops.create = True
    assert ops.symbol == "CR"
    ops.delete = True
    assert ops.symbol == "CRD"


def test_grid_renders_all_programs_and_resources():
    matrix = build_crud_matrix(_build_two_program_store())
    grid = matrix.render_grid()
    assert "LOADER" in grid
    assert "REPORTER" in grid
    assert "PROD.CUSTOMERS" in grid


# --------------------------------------------------------------------------- #
# End-to-end: the real PAYROLL fixture
# --------------------------------------------------------------------------- #


@pytest.fixture()
def payroll_store() -> NetworkXStore:
    from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
        CARTRIDGE,
    )

    s = NetworkXStore()
    adapter = CARTRIDGE.adapters()["cobol"]
    adapter.extract_kg(FIXTURE_ROOT, FIXTURE_ROOT / "PAYROLL.cbl", s)
    CARTRIDGE.ingest_kg_extras(FIXTURE_ROOT, s)
    return s


def test_payroll_end_to_end_dd_resolution(payroll_store: NetworkXStore):
    """The real PAYROLL fixture uses the DDPAYIN/DDPAYOUT convention where
    COBOL prefixes the DD name with "DD" but JCL uses the bare form. The
    resolver must handle this.
    """
    matrix = build_crud_matrix(payroll_store)

    # PAYROLL-FILE (FD) reads resolve to PROD.PAYROLL.MASTER (dataset).
    master = ResourceRef(kind="dataset", name="PROD.PAYROLL.MASTER")
    assert matrix.ops("PAYROLL", master) is not None
    assert matrix.ops("PAYROLL", master).read is True

    # OUTPUT-FILE (FD) writes resolve to PROD.PAYROLL.DAILY (dataset).
    daily = ResourceRef(kind="dataset", name="PROD.PAYROLL.DAILY")
    assert matrix.ops("PAYROLL", daily) is not None
    assert matrix.ops("PAYROLL", daily).create is True


def test_payroll_sql_block_recorded_as_write(payroll_store: NetworkXStore):
    matrix = build_crud_matrix(payroll_store)
    sql_resources = [r for r in matrix.resources() if r.kind == "sql_block"]
    assert sql_resources
    sql = sql_resources[0]
    ops = matrix.ops("PAYROLL", sql)
    assert ops is not None
    assert ops.create is True  # INSERT writes


def test_empty_store_produces_empty_matrix():
    s = NetworkXStore()
    matrix = build_crud_matrix(s)
    assert matrix.programs() == []
    assert matrix.resources() == []
