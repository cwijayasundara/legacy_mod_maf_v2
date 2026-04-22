"""AWS -> Azure service mapping.

Consumed by the translator prompt, by the verifier when setting up emulators,
and by the grapher when inferring cross-Lambda edges.
"""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.cartridge import ServiceMapping

SERVICE_MAP: list[ServiceMapping] = [
    ServiceMapping(source_kind="sqs", target_kind="service-bus-queue",
                   notes="Queue message ordering differs; use sessions if FIFO needed."),
    ServiceMapping(source_kind="sns", target_kind="event-grid-topic",
                   notes="Topic ACLs use RBAC; emit CloudEvents schema by default."),
    ServiceMapping(source_kind="dynamodb", target_kind="cosmos-db-sql",
                   notes="Partition-key / sort-key => partition-key; GSIs => composite indexes."),
    ServiceMapping(source_kind="dynamodb-streams", target_kind="cosmos-db-change-feed"),
    ServiceMapping(source_kind="s3", target_kind="blob-storage"),
    ServiceMapping(source_kind="s3-events", target_kind="event-grid-system-topic-blob"),
    ServiceMapping(source_kind="kinesis", target_kind="event-hubs"),
    ServiceMapping(source_kind="stepfunctions", target_kind="durable-functions"),
    ServiceMapping(source_kind="eventbridge", target_kind="event-grid"),
    ServiceMapping(source_kind="secretsmanager", target_kind="key-vault-secrets"),
    ServiceMapping(source_kind="ssm-parameter-store", target_kind="app-configuration"),
    ServiceMapping(source_kind="lambda-invoke", target_kind="durable-functions-activity"),
    ServiceMapping(source_kind="api-gateway", target_kind="apim-or-function-http-trigger"),
    ServiceMapping(source_kind="cloudwatch-logs", target_kind="app-insights"),
    ServiceMapping(source_kind="cloudwatch-metrics", target_kind="azure-monitor-metrics"),
    ServiceMapping(source_kind="iam-role", target_kind="managed-identity"),
    ServiceMapping(source_kind="sts-assumerole", target_kind="managed-identity-token"),
    ServiceMapping(source_kind="cognito", target_kind="entra-id"),
]


def by_source(kind: str) -> ServiceMapping | None:
    for m in SERVICE_MAP:
        if m.source_kind == kind:
            return m
    return None
