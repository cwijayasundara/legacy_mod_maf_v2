"""AWS Lambda -> Azure Function KG pipeline.

Verifies the ``source -> AST -> KG -> LLM -> migrated code`` contract on
the AWS cartridge. The platform core already exercises this pipeline
for COBOL; these tests pin the AWS-shaped variant so:

* polyglot adapters populate the shared ``KGStore`` with program /
  paragraph / external_call / dataset_ref nodes and paragraph-level
  reads/writes/calls edges;
* the cartridge's ``_run_store`` + ``ingest_kg_extras`` compose cleanly
  without requiring IaC files;
* the cartridge's ``build_translator_request`` prepends a KG-derived
  business spec onto the default AWS translator user message (so the
  LLM translator sees KG-distilled intent, not just raw source).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from maf_generic_migrator_v1.adapters.python import PythonAdapter
from maf_generic_migrator_v1.cartridges.aws_lambda_polyglot_to_azure_fn_py.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Contract
from maf_generic_migrator_v1.platform_core.kg import NetworkXStore
from maf_generic_migrator_v1.platform_core.pipeline.crud import build_crud_matrix


def _write_lambda(root: Path) -> None:
    """Seed two Lambda handlers so the classifier routes per-file.

    ``unit_classifier`` collapses a directory to a single unit only when
    it holds exactly one handler file. Writing two siblings here keeps
    the per-file unit path alive — the same one ``aws_legacy/`` exercises
    in production — so tests pin unit ids like ``handlers.order_writer``
    rather than the whole-directory id ``handlers``.
    """
    (root / "handlers").mkdir()
    (root / "handlers" / "__init__.py").write_text("", encoding="utf-8")
    (root / "handlers" / "order_writer.py").write_text(
        "import boto3\n"
        "ddb = boto3.resource('dynamodb')\n"
        "sqs = boto3.client('sqs')\n"
        "def handler(event, context):\n"
        "    table = ddb.Table('orders')\n"
        "    table.put_item(Item=event)\n"
        "    sqs.send_message(QueueUrl='q', MessageBody='done')\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (root / "handlers" / "audit_reader.py").write_text(
        "import boto3\n"
        "ddb = boto3.resource('dynamodb')\n"
        "def handler(event, context):\n"
        "    table = ddb.Table('audit')\n"
        "    return table.get_item(Key={'id': event['id']})\n",
        encoding="utf-8",
    )


def test_python_adapter_emits_program_paragraph_and_dataset_nodes(
    tmp_path: Path,
) -> None:
    _write_lambda(tmp_path)
    store = NetworkXStore()
    adapter = PythonAdapter()
    adapter.extract_kg(tmp_path, tmp_path / "handlers" / "order_writer.py", store)

    programs = [n.name for n in store.iter_nodes(kind="program")]
    assert programs == ["handlers.order_writer"]

    paragraphs = {n.name for n in store.iter_nodes(kind="paragraph")}
    # Top-level handler function must be a paragraph.
    assert "handler" in paragraphs

    external_ops = {
        tuple(n.name.split(".")) for n in store.iter_nodes(kind="external_call")
    }
    assert ("dynamodb", "put_item") in external_ops
    assert ("sqs", "send_message") in external_ops

    dataset_kinds = {
        n.attributes.get("resource_kind") for n in store.iter_nodes(kind="dataset_ref")
    }
    # Resolver returns these two for put_item + send_message.
    assert "dynamodb_table" in dataset_kinds
    assert "sqs_queue" in dataset_kinds


def test_aws_crud_matrix_attributes_reads_writes_to_program(
    tmp_path: Path,
) -> None:
    _write_lambda(tmp_path)
    store = NetworkXStore()
    PythonAdapter().extract_kg(
        tmp_path, tmp_path / "handlers" / "order_writer.py", store
    )

    matrix = build_crud_matrix(store)
    program_id = "handlers.order_writer"
    resources = matrix.resources_touched_by(program_id)
    kinds = {r.kind for r in resources}
    # Both datasets — DynamoDB (writer) + SQS (producer → writes bucket).
    assert "dataset" in kinds

    # put_item is a write; send_message is a produce-mapped write. The
    # matrix should flag at least one dataset as written.
    writers = any(
        matrix.ops(program_id, r) and matrix.ops(program_id, r).create
        for r in resources
    )
    assert writers, "CRUD matrix should see paragraph → dataset writes edges"


def test_cartridge_run_store_caches_and_contains_iac_resources(tmp_path: Path) -> None:
    _write_lambda(tmp_path)
    # Mimic a SAM-ish resource file so ingest_iac contributes something
    # without needing a full SAM template. Even when ingest_iac returns
    # nothing, the call must not crash.
    store1 = CARTRIDGE._run_store(tmp_path)
    store2 = CARTRIDGE._run_store(tmp_path)
    assert store1 is store2  # cached on repo_root

    programs = {n.name for n in store1.iter_nodes(kind="program")}
    assert "handlers.order_writer" in programs


@dataclass
class _FakeConfig:
    repo_root: Path
    workdir: Path


def test_build_translator_request_prepends_kg_business_spec(tmp_path: Path) -> None:
    _write_lambda(tmp_path)
    # Stage a workdir with the seeded source so the platform default
    # message builder has files to render.
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "order_writer.py").write_text(
        (tmp_path / "handlers" / "order_writer.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    from maf_generic_migrator_v1.platform_core.maf_workflows.unit_worker import (
        Residuals,
    )

    residuals = Residuals(
        aws_imports=[("order_writer.py", 1, "import boto3")],
        aws_sdk_calls=[("order_writer.py", 2, "boto3.resource('dynamodb')")],
        handler_shape_needs_rewrite=True,
        files_without_azure=["order_writer.py"],
    )
    item = BacklogItem(
        unit_id="handlers.order_writer",
        cartridge_id=CARTRIDGE.id,
        wave=1,
        source_paths=["handlers/order_writer.py"],
        contract=Contract(),
    )
    run_workdir = tmp_path / "run"
    run_workdir.mkdir()
    cfg = _FakeConfig(repo_root=tmp_path, workdir=run_workdir)

    # Drop any previously-cached KG on the class so this test uses the
    # fresh tmp_path fixture.
    CARTRIDGE._RUN_KG_CACHE.clear()

    result = asyncio.run(
        CARTRIDGE.build_translator_request(
            item,
            workdir,
            cfg=cfg,
            agent_bundles={},  # no bundle -> override should decline.
            residuals=residuals,
        )
    )
    # With no translator bundles, the cartridge falls back to None so the
    # platform default path handles it.
    assert result is None

    # Provide a dummy bundle and re-run. The spec block must land at the
    # very top of the user_msg (before the "Migrate this AWS Lambda..."
    # header from the default message).
    class _Bundle:
        role = "translator"
        agent = None
        spec = None

    bundles = {"translator-python": _Bundle()}
    user_msg, bundle = asyncio.run(
        CARTRIDGE.build_translator_request(
            item,
            workdir,
            cfg=cfg,
            agent_bundles=bundles,
            residuals=residuals,
        )
    )
    assert bundle is bundles["translator-python"]
    assert "KG-derived business spec" in user_msg
    # The default AWS message must still be present — we prepend, not
    # replace. Without that, the LLM would lose the residuals + source.
    assert "Migrate this AWS Lambda" in user_msg
    # Spec markdown contains the program id as its H1.
    assert "Program Spec: handlers.order_writer" in user_msg

    # Artifacts land under run_workdir (kg.json, ast/, graphs/, business_specs/)
    # + colocated per-unit spec under the unit's own workdir.
    assert (run_workdir / "kg.json").is_file()
    assert (run_workdir / "ast" / "handlers.order_writer.json").is_file()
    assert (run_workdir / "graphs" / "call_graph.mmd").is_file()
    assert (run_workdir / "graphs" / "call_graph.dot").is_file()
    assert (run_workdir / "graphs" / "dataflow.mmd").is_file()
    assert (run_workdir / "graphs" / "dataflow.dot").is_file()
    assert (run_workdir / "business_specs" / "handlers.order_writer.md").is_file()
    assert (workdir / "business_spec.md").is_file()

    # Mermaid output must start with the ```mermaid fence so GitHub renders it.
    mmd = (run_workdir / "graphs" / "call_graph.mmd").read_text(encoding="utf-8")
    assert mmd.lstrip().startswith("```mermaid"), mmd[:80]
