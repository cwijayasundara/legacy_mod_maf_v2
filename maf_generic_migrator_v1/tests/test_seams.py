"""Tests for the seam scorer."""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    NetworkXStore,
    SourceSpan,
)
from maf_generic_migrator_v1.platform_core.pipeline.seams import rank_seams


def _span() -> SourceSpan:
    return SourceSpan(file="x.cbl", start_line=1, end_line=1)


def _add_program_with_io(
    store: NetworkXStore,
    pid: str,
    *,
    reads: list[str] = (),
    writes: list[str] = (),
) -> None:
    """Helper: add a program + one paragraph + R/W edges to named datasets.
    Datasets are auto-created on first reference.
    """
    store.add_node(KGNode(id=f"program:{pid}", kind="program", name=pid, span=_span()))
    para_id = f"paragraph:{pid}:MAIN"
    store.add_node(KGNode(id=para_id, kind="paragraph", name="MAIN", span=_span()))
    store.add_edge(KGEdge(source=f"program:{pid}", target=para_id, kind="contains"))

    for ds_name in list(reads) + list(writes):
        ds_id = f"dataset_ref:{ds_name}"
        if not store.has_node(ds_id):
            store.add_node(KGNode(id=ds_id, kind="dataset_ref", name=ds_name, span=_span()))
    for ds_name in reads:
        store.add_edge(KGEdge(source=para_id, target=f"dataset_ref:{ds_name}", kind="reads"))
    for ds_name in writes:
        store.add_edge(KGEdge(source=para_id, target=f"dataset_ref:{ds_name}", kind="writes"))


def test_shared_dataset_outranks_single_consumer_resource():
    """A dataset touched by 3 programs must outrank one touched by 1.
    This is the funnel component of the score.
    """
    s = NetworkXStore()
    _add_program_with_io(s, "A", reads=["SHARED", "PRIVATE-A"])
    _add_program_with_io(s, "B", reads=["SHARED"])
    _add_program_with_io(s, "C", writes=["SHARED"])

    ranked = rank_seams(s)
    names = [c.resource.name for c in ranked]
    assert names[0] == "SHARED"


def test_pure_reader_beats_balanced_readwrite():
    """Two equally-popular datasets: one has 3 readers 0 writers (asymmetry
    1.0), the other has 2 readers 1 writer (asymmetry 0.5). The pure-reader
    seam ranks higher because CDC + replay cleanly separates one side.
    """
    s = NetworkXStore()
    # PURE-READ: 3 readers, 0 writers
    _add_program_with_io(s, "A", reads=["PURE-READ"])
    _add_program_with_io(s, "B", reads=["PURE-READ"])
    _add_program_with_io(s, "C", reads=["PURE-READ"])
    # MIXED: 2 readers, 1 writer (3 programs too)
    _add_program_with_io(s, "D", reads=["MIXED"])
    _add_program_with_io(s, "E", reads=["MIXED"])
    _add_program_with_io(s, "F", writes=["MIXED"])

    ranked = {c.resource.name: c for c in rank_seams(s)}
    pure = ranked["PURE-READ"]
    mixed = ranked["MIXED"]
    assert pure.total_score > mixed.total_score
    assert pure.asymmetry_score == 1.0
    # Mixed: |2-1|/max(2,1) = 0.5
    assert abs(mixed.asymmetry_score - 0.5) < 1e-6


def test_dataset_outranks_sql_block_outranks_file_at_equal_funnel():
    """Observable-interface priors: dataset > sql_block > file when
    everything else is equal.
    """
    s = NetworkXStore()
    # One program, three resources. Funnel is equal (1 program each).
    s.add_node(KGNode(id="program:A", kind="program", name="A", span=_span()))
    s.add_node(KGNode(id="paragraph:A:MAIN", kind="paragraph", name="MAIN", span=_span()))
    s.add_edge(KGEdge(source="program:A", target="paragraph:A:MAIN", kind="contains"))

    s.add_node(KGNode(id="dataset_ref:D1", kind="dataset_ref", name="D1", span=_span()))
    s.add_node(KGNode(id="sql_block:A:0:10", kind="sql_block", name="SQL-SELECT", span=_span(),
                      attributes={"verb": "SELECT"}))
    s.add_node(KGNode(id="file:A:FD1", kind="file", name="FD1", span=_span()))

    for target in ("dataset_ref:D1", "sql_block:A:0:10", "file:A:FD1"):
        s.add_edge(KGEdge(source="paragraph:A:MAIN", target=target, kind="reads"))

    ranked = rank_seams(s)
    kinds = [c.resource.kind for c in ranked]
    assert kinds.index("dataset") < kinds.index("sql_block") < kinds.index("file")


def test_min_programs_filters_out_singletons():
    """When asking for actual cross-program seams, single-consumer
    resources must not appear.
    """
    s = NetworkXStore()
    _add_program_with_io(s, "A", reads=["SOLO"], writes=["SHARED"])
    _add_program_with_io(s, "B", reads=["SHARED"])

    ranked = rank_seams(s, min_programs=2)
    names = [c.resource.name for c in ranked]
    assert names == ["SHARED"]  # SOLO filtered out


def test_ranking_is_stable_across_runs():
    """Same input must produce same order — no dict-iteration nondeterminism."""
    s = NetworkXStore()
    _add_program_with_io(s, "A", reads=["X"])
    _add_program_with_io(s, "B", writes=["X"])
    _add_program_with_io(s, "C", reads=["Y"])
    _add_program_with_io(s, "D", writes=["Y"])

    r1 = [c.resource.name for c in rank_seams(s)]
    r2 = [c.resource.name for c in rank_seams(s)]
    assert r1 == r2


def test_evidence_mentions_readers_and_writers():
    s = NetworkXStore()
    _add_program_with_io(s, "A", reads=["DS"])
    _add_program_with_io(s, "B", writes=["DS"])

    ranked = rank_seams(s)
    ds = next(c for c in ranked if c.resource.name == "DS")
    joined = "\n".join(ds.evidence)
    assert "A" in joined
    assert "B" in joined
    assert "readers" in joined
    assert "writers" in joined


def test_empty_store_returns_empty_ranking():
    assert rank_seams(NetworkXStore()) == []
