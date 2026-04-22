"""Dual-run fixture model and loader.

A dual-run fixture is a single (inputs → expected_outputs) tuple
captured from a reference run (mainframe, GnuCOBOL, or a staging COBOL
environment) against which the generated Java is replayed. These are
the gold standard for proving COBOL-migration correctness and are the
industry's standard gate — see "Legacy Displacement Patterns" in the
Thoughtworks / Fowler methodology.

Fixtures live on disk as JSON so they're reviewable, versionable, and
language-neutral. A minimal fixture::

    {
        "name": "happy-path-monthly",
        "target_style": "batch",
        "program_id": "PAYROLL",
        "inputs": {
            "EMP-ID": "E00042",
            "EMP-HOURS": "160",
            "EMP-RATE": "22.50"
        },
        "expected_outputs": {
            "OUT-GROSS": "3600.00"
        },
        "tolerances": {
            "OUT-GROSS": {"kind": "decimal", "pic": "9(5)V99"}
        }
    }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

TargetStyle = Literal["batch", "cics", "subroutine"]
ToleranceKind = Literal["string", "decimal", "int", "bool", "picture"]


class ToleranceSpec(BaseModel):
    """Per-field comparison rules.

    ``kind`` selects the comparator:

    * ``string`` — strict equality after optional ``trim``.
    * ``decimal`` — ``BigDecimal``-semantic comparison at the given
      ``scale`` (default derived from ``pic``); tiny absolute tolerance
      permitted via ``abs_tol``.
    * ``int`` — integer equality after optional sign normalization.
    * ``bool`` — normalized to ``True/False`` (``"Y"/"N"``, ``"1"/"0"``).
    * ``picture`` — inspects the ``pic`` clause and picks the right kind
      automatically. This is the expected default for fields derived
      from a KG ``field`` node.
    """

    kind: ToleranceKind = "picture"
    pic: str | None = None
    scale: int | None = Field(None, description="Override decimal scale (else inferred from PIC).")
    trim: bool = Field(True, description="Trim trailing spaces from strings (COBOL space-pads).")
    signed: bool = Field(False, description="Sign aware for numerics.")
    abs_tol: float = Field(0.0, ge=0.0, description="Absolute tolerance for decimal comparisons.")
    case_insensitive: bool = False


class DualRunFixture(BaseModel):
    """One golden test case."""

    name: str
    target_style: TargetStyle
    program_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: dict[str, Any] = Field(default_factory=dict)
    tolerances: dict[str, ToleranceSpec] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def tolerance_for(self, field_name: str) -> ToleranceSpec:
        """Return the tolerance for ``field_name``; default picture-based."""
        return self.tolerances.get(field_name, ToleranceSpec())


def load_fixtures(fixtures_dir: Path) -> list[DualRunFixture]:
    """Load every ``*.json`` fixture in ``fixtures_dir``.

    Returns an empty list when the dir doesn't exist — cartridges that
    haven't recorded fixtures yet should not hard-fail the dual-run gate.
    """
    if not fixtures_dir.is_dir():
        return []
    out: list[DualRunFixture] = []
    for path in sorted(fixtures_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON: {exc}") from exc
        try:
            fixture = DualRunFixture.model_validate(raw)
        except Exception as exc:  # pydantic ValidationError
            raise ValueError(f"{path}: invalid fixture: {exc}") from exc
        out.append(fixture)
    return out


__all__ = ["DualRunFixture", "TargetStyle", "ToleranceKind", "ToleranceSpec", "load_fixtures"]
