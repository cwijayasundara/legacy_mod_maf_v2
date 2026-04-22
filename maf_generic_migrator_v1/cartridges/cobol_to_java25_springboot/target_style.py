"""Target-style classifier.

Given a COBOL program node in the KG, decide which Java/Spring target
shape to generate:

* ``batch`` — the program is invoked by one or more JCL steps
  (``program -[step_of]-> step``). Maps to a Spring Batch ``Job`` with
  ``Step``s and ``Tasklet``s.
* ``cics`` — the program contains any ``txn_call`` (EXEC CICS LINK /
  XCTL / RECEIVE MAP / SEND MAP) anywhere in its paragraph tree. Maps
  to a Spring MVC ``@RestController`` served from a Spring Boot app.
* ``subroutine`` — the program is neither JCL-invoked nor CICS-bound.
  Mainframe-equivalent: a library called by other programs via
  ``CALL 'NAME'``. Maps to a plain ``@Service`` Spring bean.

CICS wins over batch when a program qualifies for both (common on
mixed-mode estates: a CICS txn that also gets batch-replayed nightly).
The translation-to-REST surface is what matters; the batch wrapper
can come later.
"""
from __future__ import annotations

from typing import Literal

from maf_generic_migrator_v1.platform_core.kg import KGStore

TargetStyle = Literal["batch", "cics", "subroutine"]


def classify_program(store: KGStore, program_id: str) -> TargetStyle:
    """Return the target style for the program named ``program_id``.

    Precedence:

    1. **CICS** — any ``txn_call`` descendant wins unconditionally
       (mixed-mode estates; the REST surface is what matters).
    2. **Subroutine** — the program declares ``PROCEDURE DIVISION USING``
       (a LINKAGE SECTION entry point). Libraries often have JCL
       wrappers for re-accrual, but the correct Java target is a
       ``@Service`` bean.
    3. **Batch** — the program is referenced by a JCL step
       (``step_of`` edge) and is NOT callable.
    4. **Subroutine** — fallback for programs with no structural
       signals either way.

    The program is identified by its ``name`` field (the ``PROGRAM-ID``
    value), not by its node id, to match how BusinessSpec identifies
    programs.
    """
    program = _find_program(store, program_id)
    if program is None:
        raise KeyError(f"no program node with name={program_id!r}")

    if _has_txn_descendant(store, program.id):
        return "cics"
    if program.attributes.get("callable") == "true":
        return "subroutine"
    if _has_step_of_edge(store, program.id):
        return "batch"
    return "subroutine"


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _find_program(store: KGStore, program_id: str):  # noqa: ANN202
    for node in store.iter_nodes(kind="program"):
        if node.name == program_id:
            return node
    return None


def _has_step_of_edge(store: KGStore, program_node_id: str) -> bool:
    return any(True for _ in store.iter_edges(source=program_node_id, kind="step_of"))


def _has_txn_descendant(store: KGStore, program_node_id: str) -> bool:
    """Any ``txn_call`` node reachable from the program via calls/contains."""
    seen: set[str] = set()
    queue: list[str] = [program_node_id]
    while queue:
        nid = queue.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for neighbor in store.neighbors(
            nid, direction="out", edge_kinds=["contains", "calls"]
        ):
            if neighbor.kind == "txn_call":
                return True
            if neighbor.id not in seen:
                queue.append(neighbor.id)
    return False


__all__ = ["TargetStyle", "classify_program"]
