"""Graph traversal helpers.

Storage-agnostic — everything here goes through the ``KGStore`` ABC. The
comprehension pipeline relies on ``post_order`` for its bottom-up summarization.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

from .schema import EdgeKind, KGNode
from .store import KGStore


def post_order(
    store: KGStore,
    root_id: str,
    *,
    edge_kinds: Iterable[EdgeKind] = ("contains",),
) -> Iterator[KGNode]:
    """DFS post-order traversal from ``root_id`` along edges of ``edge_kinds``.

    Yields each node *after* all its descendants, so the summarization pipeline
    can compose a parent's summary from already-populated children. Cycles are
    broken by tracking visited ids.
    """
    visited: set[str] = set()
    yield from _post_order(store, root_id, set(edge_kinds), visited)


def _post_order(
    store: KGStore,
    node_id: str,
    edge_kinds: set[EdgeKind],
    visited: set[str],
) -> Iterator[KGNode]:
    if node_id in visited:
        return
    visited.add(node_id)
    node = store.get_node(node_id)
    if node is None:
        return
    for child in store.neighbors(node_id, direction="out", edge_kinds=edge_kinds):
        yield from _post_order(store, child.id, edge_kinds, visited)
    yield node


def roots(store: KGStore, *, edge_kind: EdgeKind = "contains") -> list[KGNode]:
    """Return nodes that have no incoming ``edge_kind`` edge.

    For the default ``contains`` edges, these are the top-level programs/jobs —
    the natural starting points for whole-graph traversals.
    """
    out: list[KGNode] = []
    for node in store.iter_nodes():
        incoming = list(store.iter_edges(target=node.id, kind=edge_kind))
        if not incoming:
            out.append(node)
    return out


def descendants(
    store: KGStore,
    root_id: str,
    *,
    edge_kinds: Iterable[EdgeKind] = ("contains",),
    include_root: bool = False,
) -> list[KGNode]:
    """All nodes reachable from ``root_id`` via ``edge_kinds``."""
    kinds = set(edge_kinds)
    seen: set[str] = set()
    queue: list[str] = [root_id]
    result: list[KGNode] = []
    while queue:
        nid = queue.pop()
        if nid in seen:
            continue
        seen.add(nid)
        node = store.get_node(nid)
        if node is None:
            continue
        if include_root or nid != root_id:
            result.append(node)
        for child in store.neighbors(nid, direction="out", edge_kinds=kinds):
            if child.id not in seen:
                queue.append(child.id)
    return result


def ancestors(
    store: KGStore,
    node_id: str,
    *,
    edge_kinds: Iterable[EdgeKind] = ("contains",),
) -> list[KGNode]:
    """All nodes that reach ``node_id`` via ``edge_kinds`` (walking in-edges)."""
    kinds = set(edge_kinds)
    seen: set[str] = set()
    queue: list[str] = [node_id]
    result: list[KGNode] = []
    while queue:
        nid = queue.pop()
        for parent in store.neighbors(nid, direction="in", edge_kinds=kinds):
            if parent.id in seen:
                continue
            seen.add(parent.id)
            result.append(parent)
            queue.append(parent.id)
    return result
