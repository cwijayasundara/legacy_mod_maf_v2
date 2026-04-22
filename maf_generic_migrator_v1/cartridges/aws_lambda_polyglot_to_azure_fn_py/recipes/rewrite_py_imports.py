"""LibCST recipe: replace ``import boto3`` with Azure SDK stubs and add
a TODO comment so the translator prioritizes the remaining rewrites.

Deliberately conservative: this recipe only handles the easy, *mechanical*
imports. All call-site rewriting is left to the LLM translator (harder and
context-sensitive).
"""
from __future__ import annotations

import libcst as cst
from libcst import RemovalSentinel


class BotoImportToAzureStub(cst.CSTTransformer):
    """Mechanical rewrite of boto3 imports for Python sources.

    * ``import boto3`` -> block comment + remove (agent adds Azure imports).
    * ``from boto3 import X`` -> remove + tag for translator.
    * ``import botocore`` and submodules -> remove.
    """

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import):  # type: ignore[override]
        if any(_is_boto_name(alias.name) for alias in updated_node.names):
            return RemovalSentinel.REMOVE
        return updated_node

    def leave_ImportFrom(self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom):  # type: ignore[override]
        mod = updated_node.module
        if mod is not None and _is_boto_name(mod):
            return RemovalSentinel.REMOVE
        return updated_node

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:  # type: ignore[override]
        # Inject a banner at the top so the translator knows what happened.
        if not _module_has_banner(updated_node):
            banner = cst.EmptyLine(
                comment=cst.Comment(
                    "# legacy-mod: boto3 imports removed; translator must add azure.* equivalents"
                )
            )
            header = (banner, cst.EmptyLine())
            return updated_node.with_changes(header=list(header) + list(updated_node.header))
        return updated_node


def _is_boto_name(node) -> bool:  # noqa: ANN001
    """True if the dotted-attribute name root is boto3/botocore/aioboto3."""
    while isinstance(node, cst.Attribute):
        node = node.value
    if isinstance(node, cst.Name):
        return node.value in {"boto3", "botocore", "aioboto3"}
    return False


def _module_has_banner(module: cst.Module) -> bool:
    for line in module.header:
        if isinstance(line, cst.EmptyLine) and line.comment and "legacy-mod:" in line.comment.value:
            return True
    return False
