"""Test runner abstraction. Cartridges choose the command via test_framework_map."""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TestResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    skipped_reason: str | None = None

    @property
    def passed(self) -> bool:
        """Treat skipped-because-tool-missing as a soft pass.

        The cartridge layer decides whether to hard-fail on a skip; by
        default the platform lets cartridges run through missing runtime
        gates so local dev doesn't require every language's toolchain.
        """
        if self.skipped_reason is not None:
            return True
        return self.exit_code == 0

    @property
    def skipped(self) -> bool:
        return self.skipped_reason is not None


def run_pytest(workdir: Path, *, timeout: int = 300) -> TestResult:
    return _run(["pytest", "-q", "--no-header"], workdir, timeout)


def run_maven(
    workdir: Path,
    *,
    goals: tuple[str, ...] = ("-q", "compile"),
    timeout: int = 600,
) -> TestResult:
    """Run Maven in ``workdir``.

    Soft-pass when ``mvn`` isn't on ``PATH`` — lets the cartridge run on
    dev machines without a JDK/Maven while still hard-failing in CI
    where Maven is installed. Default goals do a fast compile gate; tests
    use ``goals=("-q", "test")``.
    """
    if shutil.which("mvn") is None:
        return TestResult(
            command=["mvn", *goals],
            exit_code=0,
            stdout="",
            stderr="",
            skipped_reason="mvn not on PATH — install Maven to enable the JVM gate",
        )
    if not (workdir / "pom.xml").is_file():
        return TestResult(
            command=["mvn", *goals],
            exit_code=1,
            stdout="",
            stderr=f"no pom.xml in {workdir}",
        )
    return _run(["mvn", *goals], workdir, timeout)


def run_command(cmd: list[str], workdir: Path, *, timeout: int = 300) -> TestResult:
    return _run(cmd, workdir, timeout)


def _run(cmd: list[str], workdir: Path, timeout: int) -> TestResult:
    logger.debug("running %s in %s", cmd, workdir)
    try:
        proc = subprocess.run(
            cmd, cwd=workdir, check=False, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        return TestResult(cmd, -1, exc.stdout or "", exc.stderr or f"timeout after {timeout}s")
    return TestResult(cmd, proc.returncode, proc.stdout, proc.stderr)
