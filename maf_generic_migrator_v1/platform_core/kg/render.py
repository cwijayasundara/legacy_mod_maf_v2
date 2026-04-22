"""KG visualisation renderers — DOT and Mermaid.

Two diagrams per KG:

* **Call graph** — program / paragraph control-flow. Edges:
  ``contains`` (grouping), ``performs``, ``calls``, ``step_of``.
  Matches the AST-based KG pattern from Thoughtworks' CodeConcise
  (see ``docs/architecture.md``).

* **Data-flow graph** — program / data-resource relationships.
  Edges: ``reads``, ``writes``, ``dd_of``, plus ``redefines`` for
  memory aliases. Matches the data-flow diagrams prescribed by
  Fowler's *Uncovering Mainframe Seams*.

Rendering is deterministic and side-effect-free; callers write the
returned strings to disk (and optionally shell out to ``dot`` for SVG).
"""
from __future__ import annotations

import re
from typing import Iterable

from .store import KGStore

# --------------------------------------------------------------------------- #
# Visual style per edge/node kind
# --------------------------------------------------------------------------- #

_NODE_SHAPE = {
    "program":       ("box", "#cde4ff", "bold"),
    "section":       ("box", "#e8f4ff", None),
    "paragraph":     ("box", "#f7fbff", None),
    "file":          ("cylinder", "#ffe0b3", None),
    "record":        ("note", "#fff6e0", None),
    "field":         ("ellipse", "#fffaf0", None),
    "external_call": ("trapezium", "#ffb3b3", None),
    "txn_call":      ("trapezium", "#ffccf2", None),
    "sql_block":     ("parallelogram", "#d0f0d0", None),
    "dataset_ref":   ("cylinder", "#fff0b3", "filled"),
    "job":           ("tab", "#e0d0ff", "bold"),
    "step":          ("component", "#f0e0ff", None),
}

_EDGE_STYLE = {
    "contains":    ("#666666", "solid"),
    "performs":    ("#1a7f37", "solid"),
    "calls":       ("#cc0000", "solid"),
    "step_of":     ("#0066cc", "dashed"),
    "reads":       ("#1a7f37", "solid"),
    "writes":      ("#cc6600", "solid"),
    "dd_of":       ("#0066cc", "dashed"),
    "redefines":   ("#9a00cc", "dotted"),
    "defined_in":  ("#888888", "dotted"),
    "branches_to": ("#cc0000", "dashed"),
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _safe(raw: str) -> str:
    """Coerce any KG node id into a DOT-safe identifier."""
    return re.sub(r"[^A-Za-z0-9_]", "_", raw) or "n"


def _label_for(node) -> str:  # noqa: ANN001
    """Compact, human-scannable node label."""
    if node.kind == "program":
        return f"<b>{node.name}</b>\\n(program)"
    if node.kind in ("paragraph", "section"):
        return f"{node.name}"
    if node.kind == "file":
        dd = node.attributes.get("dd_name")
        return f"{node.name}\\n(FD{f' → DD {dd}' if dd else ''})"
    if node.kind == "dataset_ref":
        return f"{node.name}\\n(dataset)"
    if node.kind in ("step", "job"):
        pgm = node.attributes.get("pgm")
        return f"{node.name}\\n(step → {pgm})" if pgm else node.name
    if node.kind == "external_call":
        return f"CALL '{node.name}'"
    if node.kind == "txn_call":
        verb = node.attributes.get("verb", "?")
        return f"EXEC CICS {verb}"
    if node.kind == "sql_block":
        verb = node.attributes.get("verb", "?")
        return f"EXEC SQL {verb}"
    return node.name


def _dot_attrs(shape: str, fill: str, extra: str | None) -> str:
    parts = [f'shape={shape}', f'fillcolor="{fill}"', 'style=filled']
    if extra == "bold":
        parts.append('penwidth=2')
    elif extra == "filled":
        parts.append('style="filled,rounded"')
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# Call graph
# --------------------------------------------------------------------------- #

_CALL_GRAPH_EDGE_KINDS: tuple[str, ...] = ("performs", "calls", "step_of")
_CALL_GRAPH_NODE_KINDS: tuple[str, ...] = (
    "program", "section", "paragraph",
    "external_call", "txn_call",
    "job", "step",
)


def call_graph_dot(store: KGStore) -> str:
    """Render the AST-derived call/control-flow graph as Graphviz DOT.

    Programs cluster their sections and paragraphs (subgraph); cross-program
    ``calls`` edges jump between clusters. JCL ``step_of`` edges show which
    steps a program answers to.
    """
    lines: list[str] = [
        "digraph call_graph {",
        "  rankdir=LR;",
        '  node [fontname="Helvetica" fontsize=10];',
        '  edge [fontname="Helvetica" fontsize=9];',
        '  compound=true;',
    ]

    # Group paragraphs / sections under their program via subgraph clusters.
    programs = sorted(store.iter_nodes(kind="program"), key=lambda n: n.name)
    rendered: set[str] = set()

    for program in programs:
        cluster_id = f"cluster_{_safe(program.id)}"
        lines.append(f'  subgraph {cluster_id} {{')
        lines.append(f'    label="{program.name}";')
        lines.append('    style="rounded,filled";')
        lines.append('    fillcolor="#f5f8fc";')

        # Program node itself.
        shape, fill, extra = _NODE_SHAPE["program"]
        lines.append(
            f'    {_safe(program.id)} [label="{_label_for(program)}" '
            f'{_dot_attrs(shape, fill, extra)}];'
        )
        rendered.add(program.id)

        # Sections + paragraphs under this program.
        from .traversal import descendants
        for kid in descendants(store, program.id, edge_kinds=("contains",)):
            if kid.kind not in _CALL_GRAPH_NODE_KINDS:
                continue
            shape, fill, extra = _NODE_SHAPE.get(kid.kind, ("box", "#ffffff", None))
            lines.append(
                f'    {_safe(kid.id)} [label="{_label_for(kid)}" '
                f'{_dot_attrs(shape, fill, extra)}];'
            )
            rendered.add(kid.id)
        lines.append("  }")

    # Jobs + steps outside any program cluster.
    for kind in ("job", "step"):
        for node in store.iter_nodes(kind=kind):
            if node.id in rendered:
                continue
            shape, fill, extra = _NODE_SHAPE.get(kind, ("box", "#ffffff", None))
            lines.append(
                f'  {_safe(node.id)} [label="{_label_for(node)}" '
                f'{_dot_attrs(shape, fill, extra)}];'
            )
            rendered.add(node.id)

    # Edges.
    for edge in store.iter_edges():
        if edge.kind not in _CALL_GRAPH_EDGE_KINDS:
            continue
        if edge.source not in rendered:
            continue
        target = store.get_node(edge.target)
        if target is None:
            continue
        # Render external-call and txn-call targets lazily (they may not be
        # under a program cluster).
        if edge.target not in rendered:
            shape, fill, extra = _NODE_SHAPE.get(target.kind, ("box", "#ffffff", None))
            lines.append(
                f'  {_safe(edge.target)} [label="{_label_for(target)}" '
                f'{_dot_attrs(shape, fill, extra)}];'
            )
            rendered.add(edge.target)
        color, style = _EDGE_STYLE.get(edge.kind, ("#000000", "solid"))
        lines.append(
            f'  {_safe(edge.source)} -> {_safe(edge.target)} '
            f'[label="{edge.kind}" color="{color}" style="{style}" fontcolor="{color}"];'
        )

    lines.append("}")
    return "\n".join(lines) + "\n"


def call_graph_mermaid(store: KGStore) -> str:
    """Render the call graph as Mermaid (GitHub natively renders this)."""
    lines: list[str] = ["```mermaid", "flowchart LR"]
    rendered: set[str] = set()
    for program in sorted(store.iter_nodes(kind="program"), key=lambda n: n.name):
        lines.append(f"  subgraph {_safe(program.id)}[{program.name}]")
        lines.append(f"    {_safe(program.id)}_self[\"<b>{program.name}</b>\"]")
        rendered.add(program.id)
        from .traversal import descendants
        for kid in descendants(store, program.id, edge_kinds=("contains",)):
            if kid.kind in _CALL_GRAPH_NODE_KINDS:
                lines.append(f'    {_safe(kid.id)}["{_label_for(kid).replace(chr(10), " / ")}"]')
                rendered.add(kid.id)
        lines.append("  end")

    for edge in store.iter_edges():
        if edge.kind not in _CALL_GRAPH_EDGE_KINDS:
            continue
        if edge.source not in rendered or edge.target not in rendered:
            # Lazily render cross-cluster targets (jobs/steps/external_call).
            for nid in (edge.source, edge.target):
                if nid in rendered:
                    continue
                node = store.get_node(nid)
                if node is None:
                    continue
                lines.append(f'  {_safe(nid)}["{_label_for(node).replace(chr(10), " / ")}"]')
                rendered.add(nid)
        lines.append(f"  {_safe(edge.source)} -->|{edge.kind}| {_safe(edge.target)}")
    lines.append("```")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Data-flow graph (Fowler-style: program × resource)
# --------------------------------------------------------------------------- #

_DATAFLOW_EDGE_KINDS: tuple[str, ...] = ("reads", "writes", "dd_of", "redefines")


def dataflow_dot(store: KGStore) -> str:
    """Render the program ↔ resource data-flow graph as DOT.

    Mirrors the "data flow diagrams showing reader/writer relationships"
    pattern Fowler's *Uncovering Mainframe Seams* prescribes. Each program
    is a box; each dataset / FD / SQL block / CICS txn is a cylinder or
    parallelogram; arrows go ``reads`` (green) or ``writes`` (orange).
    """
    lines: list[str] = [
        "digraph dataflow {",
        "  rankdir=LR;",
        '  node [fontname="Helvetica" fontsize=10];',
        '  edge [fontname="Helvetica" fontsize=9];',
    ]

    data_kinds = {"file", "dataset_ref", "sql_block", "txn_call"}
    relevant_nodes = [
        n for n in store.iter_nodes()
        if n.kind == "program" or n.kind in data_kinds
    ]

    for node in relevant_nodes:
        shape, fill, extra = _NODE_SHAPE.get(node.kind, ("box", "#ffffff", None))
        lines.append(
            f'  {_safe(node.id)} [label="{_label_for(node)}" '
            f'{_dot_attrs(shape, fill, extra)}];'
        )

    rendered_ids = {n.id for n in relevant_nodes}

    # For each program, aggregate data-flow edges from its paragraphs so
    # the diagram stays at the program granularity (matches Fowler's figures).
    from .traversal import descendants
    for program in store.iter_nodes(kind="program"):
        reached_data: dict[str, str] = {}  # data_node_id -> verb (reads|writes)
        for para in descendants(store, program.id, edge_kinds=("contains",)):
            for edge in store.iter_edges(source=para.id):
                if edge.kind not in ("reads", "writes"):
                    continue
                target = store.get_node(edge.target)
                if target is None or target.kind not in data_kinds:
                    continue
                # Write beats read — if any paragraph writes, the program writes.
                if edge.kind == "writes":
                    reached_data[target.id] = "writes"
                elif target.id not in reached_data:
                    reached_data[target.id] = "reads"

        for target_id, verb in reached_data.items():
            color, style = _EDGE_STYLE[verb]
            lines.append(
                f'  {_safe(program.id)} -> {_safe(target_id)} '
                f'[label="{verb}" color="{color}" style="{style}" fontcolor="{color}"];'
            )

    # step_of + dd_of edges — JCL-level orchestration visible in the same graph.
    for edge in store.iter_edges():
        if edge.kind not in ("dd_of", "step_of"):
            continue
        if edge.source not in rendered_ids:
            # Materialise step / job nodes.
            n = store.get_node(edge.source)
            if n is None:
                continue
            shape, fill, extra = _NODE_SHAPE.get(n.kind, ("box", "#ffffff", None))
            lines.append(
                f'  {_safe(n.id)} [label="{_label_for(n)}" '
                f'{_dot_attrs(shape, fill, extra)}];'
            )
            rendered_ids.add(n.id)
        if edge.target not in rendered_ids:
            continue
        color, style = _EDGE_STYLE.get(edge.kind, ("#000000", "solid"))
        lines.append(
            f'  {_safe(edge.source)} -> {_safe(edge.target)} '
            f'[label="{edge.kind}" color="{color}" style="{style}" fontcolor="{color}"];'
        )

    # REDEFINES edges — memory aliases, shown dotted. Only include endpoints
    # we've already rendered.
    for edge in store.iter_edges(kind="redefines"):
        if edge.source in rendered_ids and edge.target in rendered_ids:
            color, style = _EDGE_STYLE["redefines"]
            lines.append(
                f'  {_safe(edge.source)} -> {_safe(edge.target)} '
                f'[label="redefines" color="{color}" style="{style}" fontcolor="{color}"];'
            )

    lines.append("}")
    return "\n".join(lines) + "\n"


def dataflow_mermaid(store: KGStore) -> str:
    """Render the data-flow diagram as Mermaid (GitHub renders natively)."""
    lines: list[str] = ["```mermaid", "flowchart LR"]
    data_kinds = {"file", "dataset_ref", "sql_block", "txn_call"}

    for node in store.iter_nodes():
        if node.kind != "program" and node.kind not in data_kinds:
            continue
        label = _label_for(node).replace(chr(10), " / ")
        if node.kind == "dataset_ref":
            lines.append(f'  {_safe(node.id)}[("{label}")]')
        elif node.kind == "program":
            lines.append(f'  {_safe(node.id)}["<b>{label}</b>"]')
        else:
            lines.append(f'  {_safe(node.id)}["{label}"]')

    rendered_ids = {
        n.id for n in store.iter_nodes()
        if n.kind == "program" or n.kind in data_kinds
    }

    from .traversal import descendants
    for program in store.iter_nodes(kind="program"):
        reached: dict[str, str] = {}
        for para in descendants(store, program.id, edge_kinds=("contains",)):
            for edge in store.iter_edges(source=para.id):
                if edge.kind not in ("reads", "writes"):
                    continue
                target = store.get_node(edge.target)
                if target is None or target.kind not in data_kinds:
                    continue
                if edge.kind == "writes":
                    reached[target.id] = "writes"
                elif target.id not in reached:
                    reached[target.id] = "reads"
        for tid, verb in reached.items():
            lines.append(f"  {_safe(program.id)} -->|{verb}| {_safe(tid)}")

    lines.append("```")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Convenience: render all four files to a dir
# --------------------------------------------------------------------------- #


def render_all(store: KGStore, out_dir) -> dict[str, str]:  # noqa: ANN001
    """Write the four renderings to ``out_dir`` and return a summary dict.

    Files:
        call_graph.dot   .mmd
        dataflow.dot     .mmd
    """
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pairs = {
        "call_graph.dot": call_graph_dot(store),
        "call_graph.mmd": call_graph_mermaid(store),
        "dataflow.dot":   dataflow_dot(store),
        "dataflow.mmd":   dataflow_mermaid(store),
    }
    written: dict[str, str] = {}
    for name, body in pairs.items():
        path = out / name
        path.write_text(body, encoding="utf-8")
        written[name] = str(path)

    # Best-effort SVG renders if graphviz dot is on PATH.
    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("dot"):
        for dot_name in ("call_graph.dot", "dataflow.dot"):
            svg_path = out / dot_name.replace(".dot", ".svg")
            try:
                _subprocess.run(
                    ["dot", "-Tsvg", str(out / dot_name), "-o", str(svg_path)],
                    check=True, capture_output=True, timeout=30,
                )
                written[svg_path.name] = str(svg_path)
            except (_subprocess.CalledProcessError, _subprocess.TimeoutExpired):
                pass

    return written


__all__ = [
    "call_graph_dot",
    "call_graph_mermaid",
    "dataflow_dot",
    "dataflow_mermaid",
    "render_all",
]
