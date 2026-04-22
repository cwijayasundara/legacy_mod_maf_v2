"""Lightweight deterministic scorers referenced from the rubric YAMLs.

Each scorer takes (workdir: Path, item: BacklogItem) and returns a float in [0,1].
Scorers must be pure and fast — they run after every unit.
"""
from __future__ import annotations

import re
from pathlib import Path

from maf_generic_migrator_v1.platform_core.ir import BacklogItem

_AWS_SDK_IMPORT = re.compile(
    r"(?:import\s+boto3|from\s+boto3|@aws-sdk/|software\.amazon\.awssdk|Amazon\.[A-Z]\w+Client)"
)
_AZURE_SDK_IMPORT = re.compile(r"from\s+azure\.|import\s+azure\.")
_DEFAULT_CREDENTIAL = re.compile(r"DefaultAzureCredential\(")
_V2_APP_DECORATOR = re.compile(r"@app\.(?:route|service_bus_queue_trigger|event_grid_trigger|blob_trigger|timer_trigger|schedule)")


def _read_all(workdir: Path) -> str:
    """Concatenate every source file in the workdir."""
    parts: list[str] = []
    for p in workdir.rglob("*.py"):
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n\n".join(parts)


def contract_preserved(workdir: Path, item: BacklogItem) -> float:
    """Placeholder: a real implementation diffs contract inputs/outputs.

    Default: 0.9 if handler_entry produced a function; else 0.5.
    """
    text = _read_all(workdir)
    return 0.9 if "def " in text or "async def " in text else 0.5


def sdk_replaced(workdir: Path, item: BacklogItem) -> float:
    text = _read_all(workdir)
    if _AWS_SDK_IMPORT.search(text):
        return 0.0
    if _AZURE_SDK_IMPORT.search(text):
        return 1.0
    return 0.5


def handler_shape(workdir: Path, item: BacklogItem) -> float:
    text = _read_all(workdir)
    return 1.0 if _V2_APP_DECORATOR.search(text) else 0.0


def tests_parity(workdir: Path, item: BacklogItem) -> float:
    tests = list(workdir.rglob("test_*.py")) + list(workdir.rglob("*_test.py"))
    return 1.0 if tests else 0.0


def auth_managed_identity(workdir: Path, item: BacklogItem) -> float:
    text = _read_all(workdir)
    if re.search(r"(AccessKey|SharedAccessSignature|AZURE_[A-Z]+_KEY\s*=)", text):
        return 0.0
    return 1.0 if _DEFAULT_CREDENTIAL.search(text) else 0.5


def idiomatic_python(workdir: Path, item: BacklogItem) -> float:
    """Crude heuristic: penalize transliteration markers (var, let, public class, etc.)."""
    text = _read_all(workdir)
    penalty = 0
    for marker in (r"\bvar\b", r"\blet\b", r"\bpublic\s+class\b", r"\busing\s+System", r"=>\s*{"):
        if re.search(marker, text):
            penalty += 1
    return max(0.0, 1.0 - 0.2 * penalty)
