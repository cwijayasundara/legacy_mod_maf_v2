"""In-memory ``KGStore`` backed by NetworkX.

Right for tests and small repos (< ~100 programs). Neo4j backend will drop in
behind the same ``KGStore`` ABC once scale demands it.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

import networkx as nx

from .schema import EdgeKind, KGEdge, KGNode, KnowledgeGraph, NodeKind
from .store import KGStore


class NetworkXStore(KGStore):
    """NetworkX-backed KG store.

    Uses ``MultiDiGraph`` because two nodes can have multiple relations
    (e.g. a paragraph that both ``performs`` and ``calls`` another node —
    unusual in COBOL but cheap to model).
    """

    def __init__(self) -> None:
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()

    # -- Mutation --------------------------------------------------------- #

    def add_node(self, node: KGNode) -> None:
        self._g.add_node(node.id, data=node)

    def add_edge(self, edge: KGEdge) -> None:
        if not self._g.has_node(edge.source) or not self._g.has_node(edge.target):
            raise KeyError(
                f"cannot add edge {edge.source!r} -> {edge.target!r}: endpoint missing"
            )
        # De-dupe identical (source, target, kind, evidence) edges.
        for _, _, data in self._g.out_edges(edge.source, data=True):
            existing: KGEdge = data["data"]
            if (
                existing.target == edge.target
                and existing.kind == edge.kind
                and existing.evidence == edge.evidence
            ):
                return
        self._g.add_edge(edge.source, edge.target, data=edge)

    def update_node(self, node_id: str, **fields) -> None:
        node = self.get_node(node_id)
        if node is None:
            raise KeyError(f"no such node: {node_id!r}")
        updated = node.model_copy(update=fields)
        self._g.nodes[node_id]["data"] = updated

    # -- Query ------------------------------------------------------------ #

    def get_node(self, node_id: str) -> KGNode | None:
        if not self._g.has_node(node_id):
            return None
        return self._g.nodes[node_id]["data"]

    def has_node(self, node_id: str) -> bool:
        return self._g.has_node(node_id)

    def iter_nodes(self, kind: NodeKind | None = None) -> Iterator[KGNode]:
        for _, data in self._g.nodes(data=True):
            node: KGNode = data["data"]
            if kind is None or node.kind == kind:
                yield node

    def iter_edges(
        self,
        *,
        source: str | None = None,
        target: str | None = None,
        kind: EdgeKind | None = None,
    ) -> Iterator[KGEdge]:
        edge_view = self._g.edges(data=True) if source is None else self._g.out_edges(source, data=True)
        for src, dst, data in edge_view:
            edge: KGEdge = data["data"]
            if target is not None and dst != target:
                continue
            if kind is not None and edge.kind != kind:
                continue
            yield edge

    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "out",
        edge_kinds: Iterable[EdgeKind] | None = None,
    ) -> list[KGNode]:
        if not self._g.has_node(node_id):
            return []
        kinds = set(edge_kinds) if edge_kinds is not None else None
        neighbor_ids: list[str] = []
        seen: set[str] = set()

        def _collect(edges_iter):
            for src, dst, data in edges_iter:
                edge: KGEdge = data["data"]
                if kinds is not None and edge.kind not in kinds:
                    continue
                other = dst if src == node_id else src
                if other in seen:
                    continue
                seen.add(other)
                neighbor_ids.append(other)

        if direction in ("out", "both"):
            _collect(self._g.out_edges(node_id, data=True))
        if direction in ("in", "both"):
            _collect(self._g.in_edges(node_id, data=True))
        if direction not in ("out", "in", "both"):
            raise ValueError(f"direction must be 'out' | 'in' | 'both', got {direction!r}")

        return [self._g.nodes[nid]["data"] for nid in neighbor_ids]

    # -- Bulk I/O --------------------------------------------------------- #

    def snapshot(self) -> KnowledgeGraph:
        nodes = [data["data"] for _, data in self._g.nodes(data=True)]
        edges = [data["data"] for _, _, data in self._g.edges(data=True)]
        return KnowledgeGraph(nodes=nodes, edges=edges)

    def load(self, graph: KnowledgeGraph) -> None:
        self._g = nx.MultiDiGraph()
        for node in graph.nodes:
            self.add_node(node)
        for edge in graph.edges:
            self.add_edge(edge)

    def __len__(self) -> int:
        return self._g.number_of_nodes()
