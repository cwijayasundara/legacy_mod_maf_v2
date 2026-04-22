"""Checkpoint schemas for durable migration runs.

A long migration of a monolith can pause and resume across days. MAF's
checkpointing stores arbitrary state — we define the shapes here so the
platform can serialize/deserialize reliably.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..ir import BacklogItem, DependencyGraph, Inventory, PlatformBacklog

Stage = Literal["discovery", "graph", "plan", "wave", "verify", "aggregate", "done"]


class UnitResult(BaseModel):
    unit_id: str
    wave: int
    status: Literal["pending", "running", "succeeded", "failed", "skipped"]
    recipes_applied: list[str] = Field(default_factory=list)
    tests_passed: bool | None = None
    reviewer_notes: str = ""
    reviewer_verdict: str | None = None
    security_findings: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    tokens: dict[str, int] = Field(default_factory=dict)
    rubric_score: float | None = None
    error: str | None = None


class WaveResult(BaseModel):
    wave: int
    started_at: datetime
    finished_at: datetime | None = None
    items: list[BacklogItem]
    unit_results: list[UnitResult] = Field(default_factory=list)
    verification_passed: bool | None = None


class MigrationCheckpoint(BaseModel):
    run_id: str
    cartridge_id: str
    repo_root: str
    workdir: str
    stage: Stage
    started_at: datetime
    updated_at: datetime

    inventory: Inventory | None = None
    graph: DependencyGraph | None = None
    backlog: PlatformBacklog | None = None
    waves: list[WaveResult] = Field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> MigrationCheckpoint:
        return cls.model_validate_json(path.read_text())
