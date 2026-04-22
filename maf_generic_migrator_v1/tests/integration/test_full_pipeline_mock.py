"""End-to-end pipeline against the polyglot demo using MockChatClient.

Proves the full pipeline runs without credentials and produces the expected
Azure Function artifacts (function_app.py, requirements.txt, local settings,
README) for each Lambda, plus a rubric report.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from maf_generic_migrator_v1.platform_core.cartridge import load_cartridge_by_id
from maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow import WorkflowConfig, run_sync
from maf_generic_migrator_v1.platform_core.maf_workflows.mock_client import MockChatClient, materialize_mock_agents


DEMO_REPO = Path(__file__).resolve().parents[2] / "examples" / "demo_migrations" / "aws_polyglot"


def _canned_translator_response(unit_id: str) -> str:
    return f"""Here is the migrated Azure Function for `{unit_id}`:

```python path=function_app.py
import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient
from azure.cosmos import CosmosClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

_credential = DefaultAzureCredential()


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="orders",
    connection="SERVICE_BUS_CONNECTION",
)
def {unit_id}_handler(msg: func.ServiceBusMessage) -> None:
    body = json.loads(msg.get_body().decode("utf-8"))
    logging.info("processing {unit_id}: %s", body.get("orderId"))
    # ... business logic preserved from the original Lambda ...
```

```text path=requirements.txt
azure-functions>=1.21
azure-identity>=1.21
azure-servicebus>=7.13
azure-cosmos>=4.9
azure-storage-blob>=12.24
```

```json path=local.settings.json.example
{{
  "IsEncrypted": false,
  "Values": {{
    "AzureWebJobsStorage": "UseDevelopmentStorage=true",
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "SERVICE_BUS_CONNECTION": "@Microsoft.KeyVault(SecretUri=...)",
    "COSMOS_ENDPOINT": "https://your-cosmos.documents.azure.com:443/",
    "NOTIFICATIONS_TOPIC_ENDPOINT": "https://your-eventgrid.westeurope-1.eventgrid.azure.net/api/events"
  }}
}}
```

```markdown path=README.md
# {unit_id}

Migrated from AWS Lambda. Run locally with `func start` after starting Azurite
and the Service Bus Emulator.
```
"""


REVIEWER_ACCEPT = """```json
{
  "verdict": "accept",
  "findings": [],
  "suggested_fix": ""
}
```"""


SECURITY_PASS = """```json
{
  "findings": [],
  "verdict": "pass"
}
```"""


TESTER_PASS = """```json
{
  "status": "passed",
  "tests_run": 3,
  "tests_passed": 3,
  "failures": [],
  "notes": "all smoke tests green"
}
```"""


@pytest.fixture()
def polyglot_copy(tmp_path: Path) -> Path:
    """Copy the demo fixture into a temp dir so the test doesn't mutate it."""
    dst = tmp_path / "repo"
    shutil.copytree(DEMO_REPO, dst)
    return dst


def test_full_pipeline_produces_azure_artifacts(polyglot_copy: Path, tmp_path: Path) -> None:
    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")

    # Canned responses per agent name. The mock routes by inspecting the
    # system prompt for the agent-name marker injected by materialize_mock_agents.
    responses = {
        "translator-python": [_canned_translator_response("order_processor")],
        "translator-node":   [_canned_translator_response("notification_sender")],
        "translator-java":   [_canned_translator_response("invoice_generator")],
        "translator-typescript": [_canned_translator_response("ts_stub")],
        "translator-csharp":     [_canned_translator_response("cs_stub")],
        "reviewer": [REVIEWER_ACCEPT],
        "tester":   [TESTER_PASS],
        "security": [SECURITY_PASS],
    }
    client = MockChatClient(responses_by_agent=responses)
    bundles = materialize_mock_agents(cartridge, client)
    assert len(bundles) == 8  # 5 translators + reviewer + tester + security

    cfg = WorkflowConfig(
        repo_root=polyglot_copy,
        workdir=tmp_path / "wd",
        checkpoint_dir=tmp_path / "ckpt",
        skip_llm=False,
    )

    # Monkeypatch materialize_agents_lazy used by run_sync to return our mock bundles.
    import maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow as mw

    original = mw._materialize_agents_maybe
    mw._materialize_agents_maybe = lambda c, cf: bundles
    try:
        ckpt = run_sync(cartridge, cfg)
    finally:
        mw._materialize_agents_maybe = original

    assert ckpt.stage == "done"
    assert ckpt.inventory is not None and len(ckpt.inventory.units) == 3
    assert ckpt.backlog is not None and len(ckpt.backlog.waves) == 1

    # One wave with three units; each should have produced Azure artifacts.
    (wave_result,) = ckpt.waves
    unit_ids = sorted(u.unit_id for u in wave_result.unit_results)
    assert unit_ids == ["invoice_generator", "notification_sender", "order_processor"]

    for unit in wave_result.unit_results:
        unit_wd = cfg.workdir / "units" / unit.unit_id
        assert (unit_wd / "function_app.py").is_file(), f"{unit.unit_id} missing function_app.py"
        assert (unit_wd / "requirements.txt").is_file(), f"{unit.unit_id} missing requirements.txt"
        assert (unit_wd / "local.settings.json.example").is_file()
        assert (unit_wd / "README.md").is_file()
        # Reviewer accepted, so no error, and rubric should have scored.
        assert unit.status == "succeeded", f"{unit.unit_id} failed: {unit.error}"
        assert unit.reviewer_verdict == "accept"
        assert unit.rubric_score is not None and unit.rubric_score >= 0.0

    # Rubric report was written per unit.
    for unit in wave_result.unit_results:
        report = cfg.workdir / "units" / unit.unit_id / "rubric_report.json"
        assert report.is_file()


def test_reviewer_revise_triggers_second_translator_pass(tmp_path: Path) -> None:
    """Reviewer returns ``revise`` on the first pass, ``accept`` on the second.

    Uses a single-unit repo so the reviewer response queue consumption order is
    deterministic, and ``max_parallel_units=1`` to keep per-unit work serial.
    """
    # Build a one-Lambda repo so we know exactly how many reviewer calls happen.
    src_repo = tmp_path / "tiny_repo"
    (src_repo / "order_processor").mkdir(parents=True)
    (src_repo / "order_processor" / "handler.py").write_text(
        "import boto3\n"
        "sqs = boto3.client('sqs')\n"
        "def lambda_handler(event, context):\n"
        "    sqs.send_message(QueueUrl='x', MessageBody='y')\n",
        encoding="utf-8",
    )

    cartridge = load_cartridge_by_id("aws_lambda_polyglot_to_azure_fn_py")

    revise_resp = """```json
{
  "verdict": "revise",
  "findings": [{"severity": "warn", "file": "function_app.py", "line": 1, "message": "add PII redaction"}],
  "suggested_fix": "wrap logger with redaction"
}
```"""
    responses = {
        "translator-python": [
            _canned_translator_response("attempt-1"),
            _canned_translator_response("attempt-2"),
        ],
        "translator-node":       [_canned_translator_response("stub")],
        "translator-typescript": [_canned_translator_response("stub")],
        "translator-java":       [_canned_translator_response("stub")],
        "translator-csharp":     [_canned_translator_response("stub")],
        "reviewer": [revise_resp, REVIEWER_ACCEPT],
        "tester":   [TESTER_PASS],
        "security": [SECURITY_PASS],
    }
    client = MockChatClient(responses_by_agent=responses)
    bundles = materialize_mock_agents(cartridge, client)

    cfg = WorkflowConfig(
        repo_root=src_repo,
        workdir=tmp_path / "wd",
        checkpoint_dir=tmp_path / "ckpt",
        max_parallel_units=1,
    )

    import maf_generic_migrator_v1.platform_core.maf_workflows.main_workflow as mw

    original = mw._materialize_agents_maybe
    mw._materialize_agents_maybe = lambda c, cf: bundles
    try:
        ckpt = run_sync(cartridge, cfg)
    finally:
        mw._materialize_agents_maybe = original

    assert ckpt.stage == "done"
    (wave_result,) = ckpt.waves
    assert len(wave_result.unit_results) == 1
    (unit,) = wave_result.unit_results
    assert unit.reviewer_verdict == "accept"
    assert unit.status == "succeeded"
    # Translator was invoked twice: verify by checking cursor consumed.
    assert client._cursor.get("translator-python", 0) == 2
    # Reviewer consumed both responses.
    assert client._cursor.get("reviewer", 0) == 2
