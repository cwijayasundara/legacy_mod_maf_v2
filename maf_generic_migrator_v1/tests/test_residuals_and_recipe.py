"""Residual detector + LibCST recipe end-to-end."""
from __future__ import annotations

from pathlib import Path

from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Contract
from maf_generic_migrator_v1.platform_core.maf_workflows.unit_worker import compute_residuals


def _mk_item(source_paths: list[str]) -> BacklogItem:
    return BacklogItem(
        unit_id="u",
        cartridge_id="aws_lambda_polyglot_to_azure_fn_py",
        wave=1,
        source_paths=source_paths,
        contract=Contract(),
    )


def test_residuals_detect_aws_imports_and_calls(tmp_path: Path) -> None:
    src = tmp_path / "handler.py"
    src.write_text(
        "import boto3\n"
        "sqs = boto3.client('sqs')\n"
        "def lambda_handler(event, context):\n"
        "    sqs.send_message(QueueUrl='x', MessageBody='y')\n",
        encoding="utf-8",
    )
    item = _mk_item(["handler.py"])
    residuals = compute_residuals(tmp_path, item)
    assert residuals.aws_imports, "expected boto3 import to be flagged"
    assert residuals.aws_sdk_calls, "expected boto3.client(...) to be flagged"
    assert residuals.handler_shape_needs_rewrite, "expected lambda_handler to trigger shape rewrite"
    assert not residuals.empty


def test_residuals_pass_when_already_migrated(tmp_path: Path) -> None:
    src = tmp_path / "function_app.py"
    src.write_text(
        "import azure.functions as func\n"
        "from azure.identity import DefaultAzureCredential\n"
        "app = func.FunctionApp()\n"
        "@app.service_bus_queue_trigger(arg_name='msg', queue_name='q', connection='SB')\n"
        "def handler(msg):\n"
        "    return msg.get_body()\n",
        encoding="utf-8",
    )
    item = _mk_item(["function_app.py"])
    residuals = compute_residuals(tmp_path, item)
    assert not residuals.aws_imports
    assert not residuals.aws_sdk_calls
    assert not residuals.handler_shape_needs_rewrite


def test_libcst_recipe_strips_boto_imports(tmp_path: Path) -> None:
    import maf_generic_migrator_v1.recipe_backends.libcst_backend  # noqa: F401 — register
    from maf_generic_migrator_v1.recipe_backends.base import Recipe, apply_recipes

    src = tmp_path / "handler.py"
    src.write_text("import boto3\nimport os\nprint('ok')\n", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()

    recipe = Recipe(
        id="cartridges.aws_lambda_polyglot_to_azure_fn_py.recipes.BotoImportToAzureStub",
        backend="libcst",
        languages=("python",),
    )
    results = list(apply_recipes([recipe], [str(src)], workdir))
    assert results and results[0].applied, f"recipe did not apply: {results[0].error if results else 'no results'}"
    output = (workdir / "handler.py").read_text(encoding="utf-8")
    assert "import boto3" not in output
    assert "import os" in output
    assert "legacy-mod:" in output  # banner injected
