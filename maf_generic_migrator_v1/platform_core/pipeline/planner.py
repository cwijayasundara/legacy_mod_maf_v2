"""Strangler-fig wave planner.

Topological layering of units + bounded-context clustering. Pure deterministic,
no LLM. Cartridge-agnostic.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path

import networkx as nx

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge
from maf_generic_migrator_v1.platform_core.context.complexity_scorer import score_inventory
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, DependencyGraph, Inventory, PlatformBacklog
from maf_generic_migrator_v1.platform_core.kg import KGStore


class CycleError(ValueError):
    """Raised when the dependency graph contains an unbreakable cycle."""


def plan_migration(
    inventory: Inventory,
    graph: DependencyGraph,
    cartridge: MigrationCartridge,
    *,
    kg_store: KGStore | None = None,
) -> PlatformBacklog:
    """Produce migration waves from the dependency graph.

    ``kg_store`` (optional) enables **seam-aware** wave scheduling. When
    provided, the planner ranks seams (via
    ``platform_core.pipeline.seams.rank_seams``) and uses the score of
    the highest-ranked seam adjacent to each unit as the primary
    within-wave tiebreaker. Units sitting next to high-quality seams
    migrate first because they're the easiest to dual-run and canary.

    Without ``kg_store``, the planner falls back to alphabetical
    ordering within a wave — preserving pre-existing behaviour for
    cartridges that don't populate a KG (AWS Lambda et al.).
    """

    g = _to_networkx(graph)
    clusters = _cluster_bounded_contexts(g)
    seam_score_by_unit = _seam_scores_by_unit(kg_store) if kg_store is not None else {}
    waves = _schedule_waves(g, clusters, seam_score_by_unit)

    units_by_id = {u.unit_id: u for u in inventory.units}
    complexity_by_unit = score_inventory(inventory, cartridge)

    backlog_waves: list[list[BacklogItem]] = []
    for wave_index, wave_units in enumerate(waves, start=1):
        items: list[BacklogItem] = []
        for unit_id in wave_units:
            unit = units_by_id[unit_id]
            items.append(
                BacklogItem(
                    unit_id=unit_id,
                    cartridge_id=cartridge.id,
                    wave=wave_index,
                    cluster_id=clusters.get(unit_id),
                    source_paths=list(unit.files),
                    context_paths=_context_paths_for(unit_id, graph, units_by_id),
                    contract=unit.contract,
                    complexity=complexity_by_unit.get(unit_id, "MEDIUM"),
                    acceptance_criteria=_default_acceptance_criteria(unit_id, graph),
                )
            )
        backlog_waves.append(items)

    return PlatformBacklog(cartridge_id=cartridge.id, waves=backlog_waves)


def _seam_scores_by_unit(store: KGStore) -> dict[str, float]:
    """Max seam score per unit: highest ``total_score`` among seams the
    unit reads from or writes to. Programs adjacent to a top-ranked
    seam migrate first.
    """
    from .seams import rank_seams  # local import: seams depends on crud/kg

    scores: dict[str, float] = {}
    for candidate in rank_seams(store):
        for program in set(candidate.readers) | set(candidate.writers):
            if candidate.total_score > scores.get(program, 0.0):
                scores[program] = candidate.total_score
    return scores


def _to_networkx(graph: DependencyGraph) -> nx.DiGraph:
    g: nx.DiGraph = nx.DiGraph()
    for u in graph.units:
        g.add_node(u)
    for e in graph.edges:
        if e.target_unit is None:
            continue
        g.add_edge(e.source_unit, e.target_unit, kind=e.kind)
    return g


def _cluster_bounded_contexts(g: nx.DiGraph) -> dict[str, str]:
    """Units that form a strongly-connected or tightly-coupled cluster migrate together.

    Strategy: strongly connected components are one cluster each. All singletons
    get their own cluster id equal to their unit id.
    """
    clusters: dict[str, str] = {}
    for i, component in enumerate(nx.strongly_connected_components(g)):
        if len(component) > 1:
            cid = f"cluster-{i}"
            for u in component:
                clusters[u] = cid
        else:
            (u,) = component
            clusters[u] = u
    return clusters


def _schedule_waves(
    g: nx.DiGraph,
    clusters: dict[str, str],
    seam_scores: dict[str, float] | None = None,
) -> list[list[str]]:
    """Kahn-style topo sort on the cluster DAG; expand to unit ids.

    Within a wave, ordering is:

    1. Higher seam score first (when ``seam_scores`` provided) — units
       sitting next to top-ranked seams migrate first because they're
       easiest to dual-run.
    2. Alphabetical unit_id as the deterministic tiebreaker.
    """
    seam_scores = seam_scores or {}

    cluster_graph: nx.DiGraph = nx.DiGraph()
    for u in g.nodes:
        cluster_graph.add_node(clusters[u])
    for src, dst in g.edges:
        cs, cd = clusters[src], clusters[dst]
        if cs != cd:
            cluster_graph.add_edge(cs, cd)

    if not nx.is_directed_acyclic_graph(cluster_graph):
        offenders = [c for c in nx.simple_cycles(cluster_graph)]
        raise CycleError(f"cluster DAG still cyclic: {offenders!r}")

    outdeg = {n: cluster_graph.out_degree(n) for n in cluster_graph.nodes}
    layer_of_cluster: dict[str, int] = {}
    queue: deque[str] = deque(sorted(c for c, d in outdeg.items() if d == 0))
    while queue:
        c = queue.popleft()
        layer_of_cluster[c] = (
            max(
                (layer_of_cluster[n] for n in cluster_graph.successors(c) if n in layer_of_cluster),
                default=-1,
            )
            + 1
        )
        for pred in cluster_graph.predecessors(c):
            outdeg[pred] -= 1
            if outdeg[pred] == 0 and pred not in layer_of_cluster:
                queue.append(pred)

    cluster_members: dict[str, list[str]] = defaultdict(list)
    for u, c in clusters.items():
        cluster_members[c].append(u)

    layers: dict[int, list[str]] = defaultdict(list)
    for c, layer in layer_of_cluster.items():
        layers[layer].extend(cluster_members[c])

    def _sort_key(unit_id: str) -> tuple[float, str]:
        # Negative seam score so higher scores sort first; unit_id as
        # deterministic tiebreaker.
        return (-seam_scores.get(unit_id, 0.0), unit_id)

    return [sorted(layers[i], key=_sort_key) for i in sorted(layers)]


def _context_paths_for(unit_id: str, graph: DependencyGraph, units_by_id) -> list[str]:
    """Files from neighboring units whose edges suggest they're worth reading."""

    context: list[str] = []
    seen: set[str] = set()
    for e in graph.edges:
        if e.source_unit != unit_id or e.target_unit is None:
            continue
        target = units_by_id.get(e.target_unit)
        if target is None:
            continue
        for f in target.files:
            if f not in seen:
                seen.add(f)
                context.append(f)
    return context


def _default_acceptance_criteria(unit_id: str, graph: DependencyGraph) -> str:
    inbound = sum(1 for e in graph.edges if e.target_unit == unit_id)
    outbound = sum(1 for e in graph.edges if e.source_unit == unit_id)
    return (
        f"Migrate {unit_id} preserving its public contract. "
        f"Cross-unit edges: {inbound} inbound, {outbound} outbound. "
        f"All unit tests must pass. No new deprecated APIs. Rubrics green."
    )


_NODE_STYLE = {
    "module": {"shape": "box", "style": "filled", "fillcolor": "#cde4ff"},
    "library": {"shape": "folder", "style": "filled", "fillcolor": "#e8e8e8"},
    "resource": {"shape": "cylinder", "style": "filled", "fillcolor": "#ffe0b3"},
}

_EDGE_COLOR = {
    "imports": "#666666",
    "reads_from": "#1a7f37",
    "writes_to": "#b35900",
    "publishes_to": "#9a00cc",
    "queues_to": "#9a00cc",
    "invokes": "#cc0000",
    "shares_lib": "#888888",
}


def _safe_dot_id(raw: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_]", "_", raw) or "n"


def _resource_node_id(resource) -> str:
    return f"{resource.provider}_{resource.kind}_{resource.name or 'unknown'}"


def dump_graph_dot(graph: DependencyGraph, out: Path) -> None:
    """Write a Graphviz DOT file for human inspection."""
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "digraph G {",
        "  rankdir=LR;",
        '  node [fontname="Helvetica" fontsize=10];',
        '  edge [fontname="Helvetica" fontsize=9];',
    ]
    for u in graph.units:
        style = _NODE_STYLE["module"]
        attrs = " ".join(f'{k}="{v}"' for k, v in style.items())
        lines.append(f'  {_safe_dot_id(u)} [label="{u}" {attrs}];')
    resource_ids: dict[str, str] = {}
    for r in graph.resources:
        nid = _resource_node_id(r)
        style = _NODE_STYLE["resource"]
        attrs = " ".join(f'{k}="{v}"' for k, v in style.items())
        label = f"{r.kind}\\n{r.name or '?'}"
        lines.append(f'  {_safe_dot_id(nid)} [label="{label}" {attrs}];')
        resource_ids[id(r)] = nid
    for e in graph.edges:
        src = _safe_dot_id(e.source_unit)
        if e.target_unit:
            dst = _safe_dot_id(e.target_unit)
        elif e.target_resource is not None:
            dst = _safe_dot_id(resource_ids.get(id(e.target_resource)) or _resource_node_id(e.target_resource))
        else:
            continue
        color = _EDGE_COLOR.get(e.kind, "#000000")
        lines.append(f'  {src} -> {dst} [label="{e.kind}" color="{color}" fontcolor="{color}"];')
    lines.append("}")
    out.write_text("\n".join(lines))

    # Best-effort SVG render if graphviz is on PATH.
    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("dot"):
        svg = out.with_suffix(".svg")
        try:
            _subprocess.run(
                ["dot", "-Tsvg", str(out), "-o", str(svg)],
                check=True,
                capture_output=True,
                timeout=30,
            )
        except (_subprocess.CalledProcessError, _subprocess.TimeoutExpired):
            pass


def dump_graph_mermaid(graph: DependencyGraph, out: Path) -> None:
    """Write a Mermaid flowchart (renders natively on GitHub)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["```mermaid", "flowchart LR"]
    for u in graph.units:
        lines.append(f"    {_safe_dot_id(u)}[{u}]")
    resource_labels: dict[str, str] = {}
    for r in graph.resources:
        nid = _resource_node_id(r)
        label = f"{r.kind}: {r.name or '?'}"
        lines.append(f"    {_safe_dot_id(nid)}[({label})]")
        resource_labels[id(r)] = nid
    for e in graph.edges:
        src = _safe_dot_id(e.source_unit)
        if e.target_unit:
            dst = _safe_dot_id(e.target_unit)
        elif e.target_resource is not None:
            dst = _safe_dot_id(resource_labels.get(id(e.target_resource)) or _resource_node_id(e.target_resource))
        else:
            continue
        lines.append(f"    {src} -->|{e.kind}| {dst}")
    lines.append("```")
    out.write_text("\n".join(lines))
