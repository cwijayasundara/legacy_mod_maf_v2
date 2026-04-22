"""CRUD matrix over the knowledge graph.

Produces the classic ``{program × resource} → CRUDOps`` table the Fowler
mainframe-seams methodology relies on. Pure, deterministic, cartridge-agnostic
— any KG populated by an adapter's ``extract_kg`` plus JCL-or-equivalent
orchestration hints flows through this unchanged.

Key resolution: COBOL programs reference data by logical file names (FDs),
which are bound by ``SELECT … ASSIGN TO <dd-name>`` in the source and by
``// <dd-name> DD DSN=…`` in JCL. We follow that chain and attribute CRUD
ops to the real dataset rather than the internal FD name.

Sources of CRUD facts (in precedence order):

1. **Paragraph → file / sql_block / txn_call / dataset_ref ``reads`` /
   ``writes`` edges** — emitted by the COBOL adapter. These are the
   most trustworthy since they reflect actual source code.
2. **Paragraph → file with resolved dataset** — when the file has a
   ``dd_name`` attribute and the JCL ingester produced a matching
   ``dd_of`` edge, we attribute the CRUD op to the underlying dataset
   too (in addition to the FD — readers of the matrix can query either).
3. **Program → step → dataset via JCL only** — used when the source
   isn't available for a program (the JCL still tells us it touches the
   dataset, we just can't split R from W). Recorded as ``touches`` so
   callers can see it without inferring a false R/W direction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from maf_generic_migrator_v1.platform_core.kg import EdgeKind, KGStore


@dataclass(frozen=True)
class ResourceRef:
    """A resource in the CRUD matrix.

    ``kind`` is one of: ``dataset``, ``file``, ``sql_table``, ``sql_block``,
    ``txn``, ``unknown``. ``name`` is the displayable identifier — a real
    dataset name where resolvable, an FD name where not.
    """

    kind: str
    name: str

    @property
    def display(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass
class CRUDOps:
    """CRUD flags plus evidence strings ("PAYROLL.cbl:45 WRITE")."""

    create: bool = False
    read: bool = False
    update: bool = False
    delete: bool = False
    touches_only: bool = False          # JCL-derived without source R/W evidence
    evidence: list[str] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        """Compact CRUD symbol, e.g. ``"R"``, ``"CRU"``, ``"*"`` (touches_only)."""
        if self.touches_only and not (self.create or self.read or self.update or self.delete):
            return "*"
        out = []
        if self.create:
            out.append("C")
        if self.read:
            out.append("R")
        if self.update:
            out.append("U")
        if self.delete:
            out.append("D")
        return "".join(out) or "?"

    def merge(self, other: "CRUDOps") -> None:
        self.create |= other.create
        self.read |= other.read
        self.update |= other.update
        self.delete |= other.delete
        self.touches_only |= other.touches_only
        self.evidence.extend(other.evidence)


@dataclass
class CRUDMatrix:
    """Program × Resource CRUD table.

    Keyed on ``(program_id, resource)``. Resources are ``ResourceRef``
    instances (so dataset and FD names don't collide when both appear for
    the same program — e.g. ``PAYROLL`` touches both the FD
    ``PAYROLL-FILE`` and the dataset ``PROD.PAYROLL.MASTER``).
    """

    rows: dict[tuple[str, ResourceRef], CRUDOps] = field(default_factory=dict)

    # -- Lookup helpers -------------------------------------------------- #

    def programs(self) -> list[str]:
        return sorted({p for (p, _) in self.rows})

    def resources(self) -> list[ResourceRef]:
        # Sort by kind then name for stable rendering.
        return sorted({r for (_, r) in self.rows}, key=lambda r: (r.kind, r.name))

    def ops(self, program_id: str, resource: ResourceRef) -> CRUDOps | None:
        return self.rows.get((program_id, resource))

    def resources_touched_by(self, program_id: str) -> list[ResourceRef]:
        return sorted(
            (r for (p, r) in self.rows if p == program_id),
            key=lambda r: (r.kind, r.name),
        )

    def programs_touching(self, resource: ResourceRef) -> list[str]:
        return sorted({p for (p, r) in self.rows if r == resource})

    def readers_of(self, resource: ResourceRef) -> list[str]:
        return sorted(
            {p for (p, r), ops in self.rows.items() if r == resource and ops.read}
        )

    def writers_of(self, resource: ResourceRef) -> list[str]:
        return sorted(
            {
                p
                for (p, r), ops in self.rows.items()
                if r == resource and (ops.create or ops.update or ops.delete)
            }
        )

    # -- Rendering ------------------------------------------------------- #

    def render_grid(self) -> str:
        """Plain-text grid; useful in test diffs and CLI output."""
        progs = self.programs()
        resources = self.resources()
        col_w = max((len(r.display) for r in resources), default=10)
        out: list[str] = []
        header = f"{'program':<24}" + "".join(r.display.ljust(col_w + 2) for r in resources)
        out.append(header)
        for p in progs:
            line = f"{p:<24}"
            for r in resources:
                ops = self.rows.get((p, r))
                cell = ops.symbol if ops else "."
                line += cell.ljust(col_w + 2)
            out.append(line)
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #


_READ_EDGES: tuple[EdgeKind, ...] = ("reads",)
_WRITE_EDGES: tuple[EdgeKind, ...] = ("writes",)


def build_crud_matrix(store: KGStore) -> CRUDMatrix:
    """Walk the KG to produce a CRUD matrix.

    No side effects on the store. Safe to call after each re-ingestion.
    """
    matrix = CRUDMatrix()
    dd_lookup = _build_dd_lookup(store)

    for program in store.iter_nodes(kind="program"):
        pid = program.name
        # 1. Follow contains* to reach every paragraph under this program.
        paragraphs = _descendants_of_kind(store, program.id, target_kind="paragraph")
        for para in paragraphs:
            for edge in store.iter_edges(source=para.id, kind="reads"):
                _record_edge(matrix, pid, store, edge, dd_lookup, is_read=True)
            for edge in store.iter_edges(source=para.id, kind="writes"):
                _record_edge(matrix, pid, store, edge, dd_lookup, is_read=False)

        # 2. JCL-derived "touches" — program→step→dataset. Only recorded
        #    when we haven't already seen the dataset via source-driven R/W.
        for step_edge in store.iter_edges(source=program.id, kind="step_of"):
            step_id = step_edge.target
            for dd_edge in store.iter_edges(source=step_id, kind="dd_of"):
                dataset_node = store.get_node(dd_edge.target)
                if dataset_node is None or dataset_node.kind != "dataset_ref":
                    continue
                resource = ResourceRef(kind="dataset", name=dataset_node.name)
                key = (pid, resource)
                if key in matrix.rows and (
                    matrix.rows[key].read
                    or matrix.rows[key].create
                    or matrix.rows[key].update
                    or matrix.rows[key].delete
                ):
                    continue
                ops = matrix.rows.setdefault(key, CRUDOps())
                ops.touches_only = True
                if dd_edge.evidence:
                    ops.evidence.append(dd_edge.evidence)

    return matrix


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _record_edge(
    matrix: CRUDMatrix,
    program_id: str,
    store: KGStore,
    edge,  # KGEdge
    dd_lookup: dict[str, str],
    *,
    is_read: bool,
) -> None:
    """Translate one paragraph-level read/write edge into CRUD facts.

    May emit two rows (the FD and the resolved dataset) when a DD-name
    binding is known, so both COBOL-side and ops-side queries can hit the
    same matrix.
    """
    target_node = store.get_node(edge.target)
    if target_node is None:
        return

    resources: list[ResourceRef] = []

    if target_node.kind == "file":
        resources.append(ResourceRef(kind="file", name=target_node.name))
        dd_name = target_node.attributes.get("dd_name")
        if dd_name:
            dataset = _resolve_dd_to_dataset(dd_name, dd_lookup)
            if dataset is not None:
                resources.append(ResourceRef(kind="dataset", name=dataset))
    elif target_node.kind == "dataset_ref":
        resources.append(ResourceRef(kind="dataset", name=target_node.name))
    elif target_node.kind == "sql_block":
        verb = target_node.attributes.get("verb", "?")
        resources.append(ResourceRef(kind="sql_block", name=f"{program_id}:{verb}:{target_node.span.start_line if target_node.span else '?'}"))
    elif target_node.kind == "txn_call":
        verb = target_node.attributes.get("verb", "?")
        resources.append(ResourceRef(kind="txn", name=f"CICS-{verb}"))
    else:
        resources.append(ResourceRef(kind="unknown", name=target_node.name))

    for resource in resources:
        ops = matrix.rows.setdefault((program_id, resource), CRUDOps())
        if is_read:
            ops.read = True
        else:
            # COBOL's WRITE statement maps to Create semantically (appends a
            # new record). REWRITE/DELETE would set update/delete, but those
            # don't reach this function as distinct edge kinds yet — they're
            # all ``writes``. Keeping update=True also so the matrix flags
            # the resource as mutated.
            ops.create = True
            ops.update = True
        if edge.evidence:
            ops.evidence.append(edge.evidence)


def _build_dd_lookup(store: KGStore) -> dict[str, str]:
    """Build a ``DDname -> dataset_name`` map from all JCL ``dd_of`` edges.

    When the same DD name binds to multiple datasets across different jobs,
    the last one wins — an acceptable approximation for Phase 2 since
    sharing DD names across unrelated contexts is pathological in practice.
    """
    out: dict[str, str] = {}
    for edge in store.iter_edges(kind="dd_of"):
        dd_name = edge.attributes.get("dd_name")
        if not dd_name:
            continue
        target = store.get_node(edge.target)
        if target is None or target.kind != "dataset_ref":
            continue
        out[dd_name.upper()] = target.name
    return out


def _resolve_dd_to_dataset(dd_name: str, lookup: dict[str, str]) -> str | None:
    """Resolve a COBOL-side DD reference to a JCL-side dataset.

    Some sites use a ``DD`` prefix on the COBOL side that JCL drops
    (``SELECT … ASSIGN TO DDPAYIN`` ↔ ``//PAYIN DD …``). Try both forms
    before giving up.
    """
    key = dd_name.upper()
    if key in lookup:
        return lookup[key]
    if key.startswith("DD") and key[2:] in lookup:
        return lookup[key[2:]]
    return None


def _descendants_of_kind(store: KGStore, root_id: str, *, target_kind: str) -> list:
    """All nodes of ``target_kind`` reachable from ``root_id`` via ``contains`` edges."""
    out = []
    seen: set[str] = set()
    queue = [root_id]
    while queue:
        nid = queue.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for neighbor in store.neighbors(nid, direction="out", edge_kinds=["contains"]):
            if neighbor.kind == target_kind:
                out.append(neighbor)
            if neighbor.id not in seen:
                queue.append(neighbor.id)
    return out


__all__ = ["CRUDMatrix", "CRUDOps", "ResourceRef", "build_crud_matrix"]
