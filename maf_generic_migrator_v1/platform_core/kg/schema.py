"""Knowledge-graph node/edge schema.

Language-agnostic on purpose. COBOL needs richer structure than today's flat
``UnitIR`` (paragraphs, sections, data-division records, JCL steps, ...), but
Python/Java/etc. can use the same node kinds — a Python module is a ``Program``,
its functions are ``Paragraph``s, its classes are ``Section``s. Cartridges pick
the kinds that fit their source.

Nodes carry a stable ``id``, a structural ``kind``, a source span, and two
fields the comprehension pipeline populates later: ``llm_summary`` and
``embedding``. Edges are typed so graph traversals can filter by relation.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NodeKind = Literal[
    # Procedural structure
    "program",
    "section",
    "paragraph",
    "statement",
    # Data structure
    "file",         # COBOL FD / dataset handle
    "record",
    "field",
    "copybook",
    # External-world references
    "external_call",   # CALL 'OTHERPGM'
    "txn_call",        # EXEC CICS LINK / XCTL / RECEIVE MAP
    "sql_block",       # EXEC SQL ... END-EXEC
    "dataset_ref",     # VSAM/DB2/IMS handle
    # Orchestration
    "job",             # JCL job
    "step",            # JCL step
]

EdgeKind = Literal[
    "contains",        # hierarchical (Program→Paragraph, Record→Field)
    "performs",        # PERFORM X / PERFORM X THRU Y
    "calls",           # CALL / LINK / XCTL
    "reads",           # data-flow: node reads from this resource
    "writes",          # data-flow: node writes to this resource
    "defined_in",      # copybook expansion target
    "redefines",       # REDEFINES — memory alias (alt. interpretation of same bytes)
    "branches_to",     # GO TO / IF / EVALUATE target
    "step_of",         # JCL step → program
    "dd_of",           # JCL DD → dataset
]


class SourceSpan(BaseModel):
    """Where a node lives in the source tree."""

    file: str
    start_line: int = Field(..., ge=1)
    end_line: int = Field(..., ge=1)

    def covers(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


class KGNode(BaseModel):
    """A knowledge-graph node.

    Instances are mutable through the ``KGStore`` API so the comprehension
    pipeline can write back ``llm_summary`` / ``embedding`` without rebuilding
    the graph.
    """

    id: str = Field(..., description="Stable, unique across the graph.")
    kind: NodeKind
    name: str = Field(..., description="Human-readable short name (paragraph name, program id, ...).")
    span: SourceSpan | None = None
    raw_text: str | None = Field(
        None,
        description="Verbatim source slice. Optional — omit for large nodes and fetch lazily.",
    )
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Kind-specific metadata (e.g. PIC clause, SQL verb, CICS command).",
    )
    # Populated by the comprehension pipeline.
    llm_summary: str | None = None
    embedding: list[float] | None = None


class KGEdge(BaseModel):
    """A typed edge between two nodes."""

    source: str = Field(..., description="Source node id.")
    target: str = Field(..., description="Target node id.")
    kind: EdgeKind
    evidence: str | None = Field(
        None,
        description="Human-readable source of truth (file:line, JCL step, ...).",
    )
    attributes: dict[str, str] = Field(default_factory=dict)


class KnowledgeGraph(BaseModel):
    """Serializable snapshot of a KG.

    The live store (``store_networkx.NetworkXStore``) operates on mutable
    in-memory structures; this model exists for checkpointing and for passing
    graphs across process boundaries.
    """

    nodes: list[KGNode] = Field(default_factory=list)
    edges: list[KGEdge] = Field(default_factory=list)
    repo_root: str | None = None
    cartridge_id: str | None = None
