"""JCL ingester.

JCL (Job Control Language) is the IaC equivalent for MVS/zOS COBOL — it
declares job/step structure and wires programs to their datasets via ``DD``
cards. That topology is what lets the planner build strangler-fig waves for
a COBOL estate.

This module parses a minimal but real subset of JCL: ``// JOB``, ``// EXEC``
with ``PGM=``, and ``// DD`` cards with ``DSN=`` / ``DSNAME=`` / ``SYSOUT=``.
Continuation lines (``//    DSN=...``) are joined back into their logical
card before matching. Comments (``//*``) are dropped.

Produces: ``job``, ``step``, ``dataset_ref`` KG nodes plus ``contains`` /
``step_of`` / ``dd_of`` edges. Programs referenced by ``PGM=`` get
``step_of`` edges *from* their ``program`` node *to* the step that runs them
so the planner can ask "which steps invoke this program?".
"""
from __future__ import annotations

import re
from pathlib import Path

from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    KGStore,
    SourceSpan,
)

# --------------------------------------------------------------------------- #
# Regex — JCL is strict enough that a single-pass scan is fine
# --------------------------------------------------------------------------- #

_JOB_RX = re.compile(r"^//(\S+)\s+JOB\b", re.IGNORECASE)
_EXEC_RX = re.compile(r"^//(\S+)\s+EXEC\s+.*?\bPGM\s*=\s*([A-Za-z][\w#@$-]*)", re.IGNORECASE)
_DD_RX = re.compile(r"^//(\S+)\s+DD\b(.*)$", re.IGNORECASE)
_DSN_RX = re.compile(r"\bDSN(?:AME)?\s*=\s*([A-Za-z0-9@#$.\-()]+)", re.IGNORECASE)
_SYSOUT_RX = re.compile(r"\bSYSOUT\s*=\s*([A-Za-z0-9*])", re.IGNORECASE)


def _logical_cards(text: str) -> list[tuple[int, str]]:
    """Join JCL continuation lines into logical cards.

    Returns a list of ``(start_line, card_text)`` tuples. A continuation line
    is any line starting with ``//`` whose first non-blank after the slashes
    is a blank (``//   DSN=... ``).
    """
    cards: list[tuple[int, str]] = []
    pending: list[str] = []
    pending_line = 0

    def flush() -> None:
        if pending:
            cards.append((pending_line, " ".join(x.strip() for x in pending)))

    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("//*"):  # comment
            continue
        if not line.startswith("//"):
            continue
        rest = line[2:]
        if rest.startswith(" "):  # continuation
            pending.append(rest.strip())
            continue
        flush()
        pending = [line]
        pending_line = i
    flush()
    return cards


def ingest_jcl(
    repo_root: Path,
    store: KGStore,
    *,
    jcl_paths: list[Path] | None = None,
) -> None:
    """Parse JCL files under ``repo_root`` (or an explicit list) into ``store``."""

    if jcl_paths is None:
        jcl_paths = sorted(p for p in repo_root.rglob("*.jcl"))
        jcl_paths += sorted(p for p in repo_root.rglob("*.JCL"))

    for path in jcl_paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(repo_root)) if path.is_absolute() else str(path)
        _ingest_one(store, rel, text)


def _ingest_one(store: KGStore, rel: str, text: str) -> None:
    cards = _logical_cards(text)
    current_job: str | None = None
    current_job_id: str | None = None
    current_step: str | None = None
    current_step_id: str | None = None

    for start_line, card in cards:
        m = _JOB_RX.match(card)
        if m:
            current_job = m.group(1).upper()
            current_job_id = f"job:{current_job}"
            store.add_node(
                KGNode(
                    id=current_job_id,
                    kind="job",
                    name=current_job,
                    span=SourceSpan(file=rel, start_line=start_line, end_line=start_line),
                )
            )
            current_step = None
            current_step_id = None
            continue

        m = _EXEC_RX.match(card)
        if m:
            current_step = m.group(1).upper()
            pgm = m.group(2).upper()
            current_step_id = f"step:{current_job or '?'}:{current_step}"
            store.add_node(
                KGNode(
                    id=current_step_id,
                    kind="step",
                    name=current_step,
                    span=SourceSpan(file=rel, start_line=start_line, end_line=start_line),
                    attributes={"pgm": pgm},
                )
            )
            if current_job_id and store.has_node(current_job_id):
                store.add_edge(
                    KGEdge(
                        source=current_job_id,
                        target=current_step_id,
                        kind="contains",
                        evidence=f"{rel}:{start_line}",
                    )
                )
            pgm_node_id = f"program:{pgm}"
            if store.has_node(pgm_node_id):
                store.add_edge(
                    KGEdge(
                        source=pgm_node_id,
                        target=current_step_id,
                        kind="step_of",
                        evidence=f"{rel}:{start_line} PGM={pgm}",
                    )
                )
            continue

        m = _DD_RX.match(card)
        if m and current_step_id:
            dd_name = m.group(1).upper()
            body = m.group(2)
            dataset_name = _dataset_name_from_dd(body)
            if dataset_name is None:
                continue
            ds_node_id = f"dataset_ref:{dataset_name}"
            if not store.has_node(ds_node_id):
                store.add_node(
                    KGNode(
                        id=ds_node_id,
                        kind="dataset_ref",
                        name=dataset_name,
                        span=SourceSpan(file=rel, start_line=start_line, end_line=start_line),
                    )
                )
            store.add_edge(
                KGEdge(
                    source=current_step_id,
                    target=ds_node_id,
                    kind="dd_of",
                    evidence=f"{rel}:{start_line} DD {dd_name}",
                    attributes={"dd_name": dd_name},
                )
            )


def _dataset_name_from_dd(body: str) -> str | None:
    m = _DSN_RX.search(body)
    if m:
        name = m.group(1)
        # Strip parenthesized member specs like PAYROLL.MASTER(OLD).
        return name.split("(", 1)[0]
    m = _SYSOUT_RX.search(body)
    if m:
        return f"SYSOUT-{m.group(1)}"
    return None
