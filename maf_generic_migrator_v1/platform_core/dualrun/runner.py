"""Dual-run runners.

A runner takes a ``DualRunFixture`` and returns the target system's
actual outputs as a dict. Execution strategy is intentionally pluggable
— cartridges and deployment stages need different runtime bridges.

Built in:

* ``SubprocessRunner`` — spawns a configurable argv-list command with
  the fixture inputs on stdin (JSON) and parses stdout (JSON) back.
  Uses ``asyncio.create_subprocess_exec`` (argv, no shell) so there is
  no shell-injection surface.
* ``NullRunner`` — always reports ``skipped``. Useful in CI when the
  JVM / generated artifact isn't available locally — the diff engine
  still produces a readable verdict.

Runners never import MAF; they depend only on ``asyncio`` + the
standard library.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fixtures import DualRunFixture


def _binary_exists(binary: str) -> bool:
    """Resolve ``binary`` against the filesystem + PATH.

    Absolute paths are checked directly (the whole point of an absolute
    path is to bypass ``PATH``); anything else goes through
    ``shutil.which``. Bare empties are treated as missing.
    """
    if not binary:
        return False
    if os.path.isabs(binary):
        return os.path.isfile(binary) and os.access(binary, os.X_OK)
    return shutil.which(binary) is not None

logger = logging.getLogger(__name__)


@dataclass
class RunnerOutput:
    """What a runner produced for one fixture."""

    outputs: dict[str, Any] | None = None
    skipped_reason: str | None = None
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.outputs is not None and self.error is None


class Runner(ABC):
    """Base class for dual-run runners."""

    @abstractmethod
    async def run(self, fixture: DualRunFixture) -> RunnerOutput:
        """Execute ``fixture`` against the target system and return outputs."""


class NullRunner(Runner):
    """Emit ``skipped`` verdicts for every fixture.

    Use in local dev / CI pre-JVM so the dual-run stage still produces
    a structured report without actually executing.
    """

    def __init__(self, reason: str = "NullRunner: execution disabled") -> None:
        self._reason = reason

    async def run(self, fixture: DualRunFixture) -> RunnerOutput:
        return RunnerOutput(skipped_reason=self._reason, command=[])


@dataclass
class SubprocessRunner(Runner):
    """Run each fixture as an argv-list subprocess invocation.

    ``command`` is an argv list — no shell is invoked. Template tokens
    ``{program_id}`` / ``{fixture_name}`` / ``{target_style}`` /
    ``{workdir}`` are substituted at call time via ``str.format``. The
    process gets ``fixture.inputs`` as a JSON document on stdin and is
    expected to emit ``actual_outputs`` as JSON on stdout.

    When the binary isn't on ``PATH``, the runner returns a clean skip
    rather than an error — same soft-pass discipline as ``run_maven``.
    """

    command: list[str]
    workdir: Path | None = None
    timeout_s: float = 60.0

    async def run(self, fixture: DualRunFixture) -> RunnerOutput:
        rendered = [self._interp(arg, fixture) for arg in self.command]
        binary = rendered[0] if rendered else ""
        if not _binary_exists(binary):
            return RunnerOutput(
                skipped_reason=f"command not on PATH: {binary!r}",
                command=rendered,
            )

        stdin_payload = json.dumps(fixture.inputs).encode("utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *rendered,
                cwd=str(self.workdir) if self.workdir else None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return RunnerOutput(error=f"exec failed: {exc}", command=rendered)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(stdin_payload), timeout=self.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            return RunnerOutput(
                error=f"timeout after {self.timeout_s}s",
                command=rendered,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            return RunnerOutput(
                error=f"exit={proc.returncode}",
                stdout=stdout,
                stderr=stderr,
                command=rendered,
            )

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            return RunnerOutput(
                error=f"stdout is not JSON: {exc}",
                stdout=stdout,
                stderr=stderr,
                command=rendered,
            )
        if not isinstance(parsed, dict):
            return RunnerOutput(
                error="stdout JSON is not an object",
                stdout=stdout,
                stderr=stderr,
                command=rendered,
            )
        return RunnerOutput(
            outputs=parsed,
            stdout=stdout,
            stderr=stderr,
            command=rendered,
        )

    def _interp(self, template: str, fixture: DualRunFixture) -> str:
        """Manual token substitution — ``str.format`` would choke on any
        other ``{...}`` content (embedded Python dict literals, shell
        substitutions, JSON fragments) that happens to appear in argv.
        """
        replacements = {
            "{program_id}": fixture.program_id,
            "{fixture_name}": fixture.name,
            "{target_style}": fixture.target_style,
            "{workdir}": str(self.workdir) if self.workdir else "",
        }
        out = template
        for token, value in replacements.items():
            if token in out:
                out = out.replace(token, value)
        return out


__all__ = ["NullRunner", "Runner", "RunnerOutput", "SubprocessRunner"]
