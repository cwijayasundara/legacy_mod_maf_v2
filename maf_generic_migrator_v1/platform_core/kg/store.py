"""KG storage interface.

Concrete backends (NetworkX in-memory, Neo4j, ...) implement ``KGStore``.
The comprehension pipeline and retrieval code depend only on this ABC.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator

from .schema import EdgeKind, KGEdge, KGNode, KnowledgeGraph, NodeKind


class KGStore(ABC):
    """Storage-agnostic KG interface."""

    # -- Mutation --------------------------------------------------------- #

    @abstractmethod
    def add_node(self, node: KGNode) -> None:
        """Insert or replace a node by id."""

    @abstractmethod
    def add_edge(self, edge: KGEdge) -> None:
        """Insert an edge. Idempotent if an identical edge already exists."""

    @abstractmethod
    def update_node(self, node_id: str, **fields) -> None:
        """Merge ``fields`` into the node with id ``node_id``.

        Used primarily by the comprehension pipeline to write back
        ``llm_summary`` and ``embedding`` without rebuilding the graph.
        """

    # -- Query ------------------------------------------------------------ #

    @abstractmethod
    def get_node(self, node_id: str) -> KGNode | None:
        """Return the node with ``node_id`` or ``None`` if missing."""

    @abstractmethod
    def has_node(self, node_id: str) -> bool:
        ...

    @abstractmethod
    def iter_nodes(self, kind: NodeKind | None = None) -> Iterator[KGNode]:
        """Yield nodes, optionally filtered by kind."""

    @abstractmethod
    def iter_edges(
        self,
        *,
        source: str | None = None,
        target: str | None = None,
        kind: EdgeKind | None = None,
    ) -> Iterator[KGEdge]:
        """Yield edges matching any combination of source/target/kind filters."""

    @abstractmethod
    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "out",
        edge_kinds: Iterable[EdgeKind] | None = None,
    ) -> list[KGNode]:
        """Return adjacent nodes.

        ``direction`` is one of ``"out"`` (follow edges away), ``"in"`` (follow
        edges into the node), or ``"both"``. ``edge_kinds``, when set, limits
        the relations walked.
        """

    # -- Bulk I/O --------------------------------------------------------- #

    @abstractmethod
    def snapshot(self) -> KnowledgeGraph:
        """Return a serializable copy of the current state."""

    @abstractmethod
    def load(self, graph: KnowledgeGraph) -> None:
        """Populate this store from a snapshot, replacing prior state."""

    def __len__(self) -> int:
        """Number of nodes in the store. Not an ABC method; backends may override."""
        return sum(1 for _ in self.iter_nodes())
