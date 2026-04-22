"""Deterministic recipes for the AWS -> Azure cartridge.

These run *before* the LLM translator and handle the mechanical parts of the
migration (imports, known SDK call patterns, manifest files).
"""

from .rewrite_py_imports import BotoImportToAzureStub

__all__ = ["BotoImportToAzureStub"]
