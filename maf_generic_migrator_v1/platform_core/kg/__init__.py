"""Knowledge graph: structural representation of source beyond flat ``UnitIR``.

COBOL and other legacy systems need sub-unit granularity (paragraphs, records,
JCL steps, copybook expansions) and typed relations (performs, calls, reads,
writes) that the language-agnostic IR can't express. The KG layer fills that
gap. Language adapters populate it; the comprehension pipeline enriches it;
the planner queries it for CRUD matrices and seam analysis; translators
retrieve from it at generation time.

Design invariant: the KG is storage-agnostic. ``KGStore`` is the interface;
``NetworkXStore`` is the in-memory default. A Neo4j backend drops in for
larger codebases without touching callers.
"""
from maf_generic_migrator_v1.platform_core.kg.schema import (
    EdgeKind,
    KGEdge,
    KGNode,
    KnowledgeGraph,
    NodeKind,
    SourceSpan,
)
from maf_generic_migrator_v1.platform_core.kg.store import KGStore
from maf_generic_migrator_v1.platform_core.kg.store_networkx import NetworkXStore
from maf_generic_migrator_v1.platform_core.kg.render import (
    call_graph_dot,
    call_graph_mermaid,
    dataflow_dot,
    dataflow_mermaid,
    render_all as render_graphs,
)
from maf_generic_migrator_v1.platform_core.kg.traversal import (
    ancestors,
    descendants,
    post_order,
    roots,
)

__all__ = [
    "EdgeKind",
    "KGEdge",
    "KGNode",
    "KGStore",
    "KnowledgeGraph",
    "NetworkXStore",
    "NodeKind",
    "SourceSpan",
    "ancestors",
    "call_graph_dot",
    "call_graph_mermaid",
    "dataflow_dot",
    "dataflow_mermaid",
    "descendants",
    "post_order",
    "render_graphs",
    "roots",
]
