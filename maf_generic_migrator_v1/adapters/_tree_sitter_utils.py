"""Tree-sitter helpers shared across language adapters.

Provides:
  * ``get_ts_parser(language)`` — lazy-loaded parser via tree-sitter-language-pack
  * ``walk_nodes(node)`` — pre-order traversal
  * ``node_text(node, source)`` — slice source bytes by node range
  * ``find_nodes_by_type(root, kinds)`` — iterate matching nodes
  * ``line_of(node)`` — 1-based starting line

Pack name mapping: we translate our language ids to the pack's names.
"""
from __future__ import annotations

import functools
from collections.abc import Iterator

_PACK_NAME = {
    "python": "python",
    "node": "javascript",
    "typescript": "typescript",
    "java": "java",
    "csharp": "csharp",
    "go": "go",
    "ruby": "ruby",
    "rust": "rust",
    "cobol": "cobol",
}


@functools.lru_cache(maxsize=None)
def get_ts_parser(language: str):  # noqa: ANN201 — returns tree_sitter.Parser
    """Return a cached tree-sitter ``Parser`` for the given language."""
    from tree_sitter_language_pack import get_parser

    pack_name = _PACK_NAME.get(language, language)
    return get_parser(pack_name)


def parse(language: str, source: str) -> tuple[object | None, bytes]:
    """Parse ``source`` using the language's tree-sitter grammar.

    Returns ``(root_node, source_bytes)`` or ``(None, b"")`` if the grammar
    is unavailable on this host.
    """
    try:
        parser = get_ts_parser(language)
    except Exception:  # noqa: BLE001 — grammar may not be installed
        return None, b""
    data = source.encode("utf-8", errors="replace")
    tree = parser.parse(data)
    return tree.root_node, data


def walk_nodes(node) -> Iterator:  # noqa: ANN001, ANN201
    """Pre-order traversal."""
    yield node
    for child in node.children:
        yield from walk_nodes(child)


def find_nodes_by_type(root, kinds: set[str]) -> Iterator:  # noqa: ANN001, ANN201
    for node in walk_nodes(root):
        if node.type in kinds:
            yield node


def node_text(node, source_bytes: bytes) -> str:  # noqa: ANN001
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def line_of(node) -> int:  # noqa: ANN001
    """1-based starting line of a tree-sitter node."""
    return node.start_point[0] + 1
