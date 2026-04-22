"""Dependency graph builder.

Deterministic-first: static evidence from adapters + cartridge-provided
grapher hints (IaC, serverless manifests, ...). LLM fallback is reserved for
explicitly ambiguous edges. The grapher itself is pure and cartridge-agnostic.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from maf_generic_migrator_v1.platform_core.cartridge import MigrationCartridge
from maf_generic_migrator_v1.platform_core.ir import CrossEdge, DependencyGraph, Inventory, Resource

logger = logging.getLogger(__name__)


def build_graph(inventory: Inventory, cartridge: MigrationCartridge) -> DependencyGraph:
    """Build a ``DependencyGraph`` from inventory + cartridge hints."""

    units_by_id = {u.unit_id: u for u in inventory.units}
    edges: list[CrossEdge] = []
    ambiguous: list[CrossEdge] = []
    resources: dict[str, Resource] = {}

    # --- 1. Intra-unit evidence from adapters -----------------------------
    for unit in inventory.units:
        for imp in unit.imports:
            if imp.is_external:
                continue
            # Map import module to another unit in the repo. The adapter may
            # have already set unit-qualified module names; fall back to simple
            # match on the leading segment.
            target = _import_to_unit_id(imp.module, units_by_id)
            if target and target != unit.unit_id:
                edges.append(
                    CrossEdge(
                        source_unit=unit.unit_id,
                        target_unit=target,
                        kind="imports",
                        evidence=f"{imp.file}:{imp.line} import {imp.module}",
                    )
                )

        for call in unit.service_calls:
            res = call.resource
            if res is None:
                ambiguous.append(
                    CrossEdge(
                        source_unit=unit.unit_id,
                        target_unit=None,
                        kind="invokes",
                        evidence=f"{call.file}:{call.line} {call.service}.{call.operation} (unresolved)",
                        confidence=0.3,
                    )
                )
                continue

            rid = _resource_id(res)
            resources.setdefault(rid, res)

            kind = _edge_kind_for_service_call(call.service, call.operation)
            edges.append(
                CrossEdge(
                    source_unit=unit.unit_id,
                    target_resource=res,
                    kind=kind,
                    evidence=f"{call.file}:{call.line} {call.service}.{call.operation}",
                )
            )

    # --- 2. Cartridge hints (IaC ingestion, serverless manifests, ...) ----
    for hint in cartridge.grapher_hints(inventory):
        edges.extend(_materialize_hint(hint))

    # --- 3. Resolve ambiguous edges via resource -> unit backlinks --------
    #   If multiple units touch the same resource, we have inferred cross-unit
    #   edges (queue A pub/sub, shared table, ...).
    edges.extend(_derive_cross_unit_from_resources(edges, resources))

    return DependencyGraph(
        units=[u.unit_id for u in inventory.units],
        resources=list(resources.values()),
        edges=edges,
        ambiguous_edges=ambiguous,
    )


def _import_to_unit_id(module: str, units_by_id: dict) -> str | None:
    if not module or module.startswith("."):
        return None
    head = module.split(".", 1)[0]
    return head if head in units_by_id else None


def _resource_id(r: Resource) -> str:
    return f"{r.provider}:{r.kind}:{r.name or r.identifier or '?'}"


def _edge_kind_for_service_call(service: str, operation: str) -> str:
    op = operation.lower()
    if service in {"sqs", "service-bus"} and ("send" in op or "publish" in op):
        return "queues_to"
    if service in {"sns", "event-grid", "eventbridge"} and ("publish" in op or "put" in op):
        return "publishes_to"
    if "get" in op or "read" in op or "scan" in op or "query" in op:
        return "reads_from"
    if "put" in op or "write" in op or "update" in op or "insert" in op or "delete" in op:
        return "writes_to"
    return "invokes"


def _derive_cross_unit_from_resources(edges: list[CrossEdge], resources: dict) -> list[CrossEdge]:
    """If unit A writes to queue Q and unit B reads from queue Q, emit A -> B edge."""

    writers: defaultdict[str, list[str]] = defaultdict(list)
    readers: defaultdict[str, list[str]] = defaultdict(list)
    for e in edges:
        if e.target_resource is None:
            continue
        rid = _resource_id(e.target_resource)
        if e.kind in {"queues_to", "publishes_to", "writes_to"}:
            writers[rid].append(e.source_unit)
        elif e.kind in {"reads_from", "invokes"}:
            readers[rid].append(e.source_unit)

    derived: list[CrossEdge] = []
    for rid, ws in writers.items():
        for w in ws:
            for r in readers.get(rid, []):
                if w == r:
                    continue
                derived.append(
                    CrossEdge(
                        source_unit=w,
                        target_unit=r,
                        target_resource=resources.get(rid),
                        kind="invokes",
                        evidence=f"derived via shared resource {rid}",
                        confidence=0.9,
                    )
                )
    return derived


def _materialize_hint(hint: dict) -> list[CrossEdge]:
    """Convert a cartridge hint dict into ``CrossEdge`` instances.

    Expected hint shape::

        {
            "source_unit": "...",
            "target_unit": "..." | None,
            "target_resource": {...} | None,
            "kind": "queues_to",
            "evidence": "serverless.yml:lambda.events[0].sqs",
        }
    """
    try:
        target_resource = None
        if hint.get("target_resource"):
            target_resource = Resource(**hint["target_resource"])
        return [
            CrossEdge(
                source_unit=hint["source_unit"],
                target_unit=hint.get("target_unit"),
                target_resource=target_resource,
                kind=hint["kind"],
                evidence=hint.get("evidence", "cartridge hint"),
                confidence=hint.get("confidence", 1.0),
            )
        ]
    except Exception:  # noqa: BLE001
        logger.warning("ignoring malformed grapher hint: %r", hint, exc_info=True)
        return []
