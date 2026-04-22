"""Language-agnostic Intermediate Representation.

Every source-language adapter emits values of these types. The platform core
(discovery, grapher, planner, executor) operates only on IR, not on raw source.
This is what lets a single platform drive any cartridge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

UnitKind = Literal["module", "class", "function", "package", "service", "lambda", "component"]
TriggerKind = Literal["http", "queue", "topic", "schedule", "stream", "blob", "timer", "manual"]
EdgeKind = Literal[
    "imports",          # static import
    "calls",            # direct function / method call
    "invokes",          # cross-unit runtime invocation (http, rpc)
    "queues_to",        # writes to an intermediate queue
    "publishes_to",     # publishes to a topic / event bus
    "reads_from",       # reads from a table / store
    "writes_to",        # writes to a table / store
    "shares_lib",       # shared library / layer
    "contract_input",   # unit accepts this resource as input
    "contract_output",  # unit produces this resource as output
]


class Resource(BaseModel):
    """An external resource a unit touches: a queue, topic, table, bucket, secret, etc."""

    kind: str = Field(..., description="'queue' | 'topic' | 'table' | 'bucket' | 'secret' | 'api' | ...")
    provider: str = Field(..., description="'aws' | 'azure' | 'gcp' | 'local' | 'onprem' | ...")
    name: str | None = Field(None, description="Literal name/ARN when extractable")
    identifier: str | None = Field(None, description="Canonical id if known (ARN, URL, ResourceId)")
    extra: dict[str, str] = Field(default_factory=dict)


class ServiceCall(BaseModel):
    """A normalized external-service call site.

    Adapters extract these for SDK calls (boto3, aws-sdk-js, AWSSDK.NET, java-sdk v2),
    HTTP client calls, database driver calls, etc.
    """

    service: str = Field(..., description="'sqs' | 'dynamodb' | 'http' | 'postgres' | ...")
    operation: str = Field(..., description="SDK method name or HTTP verb, e.g. 'send_message', 'GET'")
    resource: Resource | None = None
    file: str
    line: int


class Trigger(BaseModel):
    """How a unit is invoked."""

    kind: TriggerKind
    source: str | None = None  # queue name, schedule expression, http route, ...
    extra: dict[str, str] = Field(default_factory=dict)


class Import(BaseModel):
    module: str
    file: str
    line: int
    is_external: bool = False


class Contract(BaseModel):
    """The public contract of a unit.

    Used by the planner to figure out seams and facades, and by translators to
    ensure the migrated unit preserves the contract.
    """

    inputs: list[Resource] = Field(default_factory=list)
    outputs: list[Resource] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    env_refs: list[str] = Field(default_factory=list, description="Env vars read at runtime")
    notes: str | None = None


class UnitIR(BaseModel):
    """Normalized representation of one migration unit (file, module, Lambda, package, class, ...).

    A cartridge defines what counts as a "unit" via its adapters. For the AWS Lambda
    cartridge, a unit = one Lambda function. For Python-2-to-3, a unit = one .py module.
    """

    unit_id: str = Field(..., description="Unique stable id within the repo")
    kind: UnitKind
    language: str
    language_version: str | None = None

    root_path: str = Field(..., description="Unit root directory (relative to repo root)")
    handler_entry: str | None = Field(None, description="'file.py:handler' or entry identifier")
    files: list[str] = Field(default_factory=list, description="Source files belonging to this unit")

    imports: list[Import] = Field(default_factory=list)
    service_calls: list[ServiceCall] = Field(default_factory=list)
    contract: Contract = Field(default_factory=Contract)

    loc: int = 0
    byte_size: int = 0
    extra: dict[str, str] = Field(default_factory=dict)

    def file_paths(self, repo_root: Path) -> list[Path]:
        return [repo_root / f for f in self.files]


class CrossEdge(BaseModel):
    """An edge between two units, or between a unit and a resource."""

    source_unit: str
    target_unit: str | None = None
    target_resource: Resource | None = None
    kind: EdgeKind
    evidence: str = Field(..., description="Human-readable source of truth for this edge")
    confidence: float = Field(1.0, ge=0.0, le=1.0)


class Inventory(BaseModel):
    """The output of discovery: every unit in the repo, normalized."""

    repo_root: str
    cartridge_id: str
    units: list[UnitIR]
    scan_warnings: list[str] = Field(default_factory=list)


class DependencyGraph(BaseModel):
    """The output of the grapher: units + edges."""

    units: list[str]                       # unit_ids
    resources: list[Resource] = Field(default_factory=list)
    edges: list[CrossEdge]
    ambiguous_edges: list[CrossEdge] = Field(default_factory=list, description="Flagged for LLM resolution")


class BacklogItem(BaseModel):
    """One work item dispatched to a unit-worker subagent."""

    unit_id: str
    cartridge_id: str
    wave: int
    cluster_id: str | None = None
    source_paths: list[str]
    context_paths: list[str] = Field(default_factory=list, description="Read-only reference files (shared libs, neighbors)")
    contract: Contract
    complexity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    acceptance_criteria: str = ""
    facade_inbound: str | None = None
    facade_outbound: str | None = None


class PlatformBacklog(BaseModel):
    """Ordered migration waves."""

    cartridge_id: str
    waves: list[list[BacklogItem]]
