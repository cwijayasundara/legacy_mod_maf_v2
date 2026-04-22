"""Deterministic ingestion of IaC files to seed cross-Lambda edges.

Supports:
  * SAM templates (``template.yaml`` / ``template.yml``)
  * Serverless Framework (``serverless.yml``)
  * CDK synthesized output (``cdk.out/*.template.json``) — best effort
  * CloudFormation JSON/YAML

For each Lambda -> resource wiring we emit grapher hints the platform turns
into ``CrossEdge`` objects.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover — listed in pyproject
    yaml = None  # type: ignore[assignment]


def ingest_iac(repo_root: Path) -> list[dict[str, Any]]:
    """Walk ``repo_root`` for IaC files and return grapher hints."""

    hints: list[dict[str, Any]] = []

    for path in repo_root.rglob("template.y*ml"):
        hints.extend(_from_sam(path, repo_root))
    for path in repo_root.rglob("serverless.y*ml"):
        hints.extend(_from_serverless(path, repo_root))
    for path in repo_root.rglob("*.template.json"):
        hints.extend(_from_cloudformation(path, repo_root))

    return hints


def _safe_load_yaml(path: Path) -> dict[str, Any] | None:
    if yaml is None:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _from_sam(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    doc = _safe_load_yaml(path)
    if not isinstance(doc, dict):
        return []
    resources = doc.get("Resources", {})
    hints: list[dict[str, Any]] = []
    for logical_id, spec in resources.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("Type") != "AWS::Serverless::Function":
            continue
        events = spec.get("Properties", {}).get("Events", {}) or {}
        for ev_name, ev in events.items():
            hint = _event_to_hint(logical_id, ev)
            if hint:
                hint["evidence"] = f"{path.relative_to(repo_root)}: {logical_id}.Events.{ev_name}"
                hints.append(hint)
    return hints


def _from_serverless(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    doc = _safe_load_yaml(path)
    if not isinstance(doc, dict):
        return []
    functions = doc.get("functions", {}) or {}
    hints: list[dict[str, Any]] = []
    for fname, spec in functions.items():
        if not isinstance(spec, dict):
            continue
        for ev in spec.get("events", []) or []:
            if not isinstance(ev, dict):
                continue
            for ev_type, ev_spec in ev.items():
                hint = _event_to_hint(fname, {"Type": ev_type.capitalize(), "Properties": ev_spec or {}})
                if hint:
                    hint["evidence"] = f"{path.relative_to(repo_root)}: {fname}.events.{ev_type}"
                    hints.append(hint)
    return hints


def _from_cloudformation(path: Path, repo_root: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(doc, dict):
        return []
    # CDK / CFN event sources are richer than SAM; just capture direct AWS::Lambda::EventSourceMapping
    hints: list[dict[str, Any]] = []
    resources = doc.get("Resources", {})
    for logical_id, spec in resources.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("Type") == "AWS::Lambda::EventSourceMapping":
            props = spec.get("Properties", {})
            hint = _eventsource_mapping_hint(props)
            if hint:
                hint["evidence"] = f"{path.relative_to(repo_root)}: {logical_id}"
                hints.append(hint)
    return hints


def _event_to_hint(function_id: str, ev: dict[str, Any]) -> dict[str, Any] | None:
    t = (ev.get("Type") or "").lower()
    props = ev.get("Properties") or {}
    if t == "sqs":
        return {
            "source_unit": function_id,
            "target_unit": None,
            "target_resource": {
                "kind": "queue",
                "provider": "aws",
                "name": _extract_arn_name(props.get("Queue") or props.get("arn")),
            },
            "kind": "reads_from",
        }
    if t in {"sns", "topic"}:
        return {
            "source_unit": function_id,
            "target_unit": None,
            "target_resource": {
                "kind": "topic",
                "provider": "aws",
                "name": _extract_arn_name(props.get("Topic") or props.get("arn")),
            },
            "kind": "reads_from",
        }
    if t in {"dynamodb", "stream"}:
        return {
            "source_unit": function_id,
            "target_unit": None,
            "target_resource": {
                "kind": "table",
                "provider": "aws",
                "name": _extract_arn_name(props.get("Stream") or props.get("arn")),
            },
            "kind": "reads_from",
        }
    if t == "s3":
        return {
            "source_unit": function_id,
            "target_unit": None,
            "target_resource": {
                "kind": "bucket",
                "provider": "aws",
                "name": props.get("Bucket"),
            },
            "kind": "reads_from",
        }
    if t == "schedule":
        return {
            "source_unit": function_id,
            "target_unit": None,
            "target_resource": {
                "kind": "schedule",
                "provider": "aws",
                "name": props.get("Schedule"),
            },
            "kind": "reads_from",
        }
    if t in {"api", "apigateway", "http"}:
        return {
            "source_unit": function_id,
            "target_unit": None,
            "target_resource": {
                "kind": "api",
                "provider": "aws",
                "name": props.get("Path"),
            },
            "kind": "reads_from",
        }
    return None


def _eventsource_mapping_hint(props: dict) -> dict | None:
    arn = props.get("EventSourceArn")
    if not arn:
        return None
    kind, name = _kind_from_arn(arn)
    return {
        "source_unit": props.get("FunctionName", "?"),
        "target_unit": None,
        "target_resource": {"kind": kind, "provider": "aws", "name": name},
        "kind": "reads_from",
    }


def _extract_arn_name(value) -> str | None:
    if isinstance(value, str):
        # arn:aws:sqs:region:acct:queueName
        return value.rsplit(":", 1)[-1]
    if isinstance(value, dict) and "Fn::GetAtt" in value:
        target = value["Fn::GetAtt"]
        if isinstance(target, list) and target:
            return str(target[0])
    return None


def _kind_from_arn(arn: str) -> tuple[str, str]:
    parts = arn.split(":")
    if len(parts) < 6:
        return "resource", arn
    service = parts[2]
    name = parts[-1]
    return {"sqs": "queue", "sns": "topic", "dynamodb": "table", "kinesis": "stream"}.get(service, service), name
