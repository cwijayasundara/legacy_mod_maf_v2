"""Tests for the Maven runner and the cartridge ``verify_unit`` gate.

When ``mvn`` is on PATH we do a real smoke-compile on a trivial pom.
When it isn't we assert the soft-pass path works cleanly — the
cartridge must not hard-fail developers who haven't installed Maven.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Contract
from maf_generic_migrator_v1.platform_core.tools.test_runner import run_maven

MVN_AVAILABLE = shutil.which("mvn") is not None


def _item(unit_id: str = "TEST") -> BacklogItem:
    return BacklogItem(
        unit_id=unit_id,
        cartridge_id="cobol_to_java25_springboot",
        wave=1,
        source_paths=[],
        contract=Contract(),
    )


def test_run_maven_skips_when_mvn_missing(tmp_path: Path, monkeypatch):
    """If ``mvn`` isn't on PATH, run_maven must soft-pass with an
    explanatory ``skipped_reason`` rather than raising or hard-failing.
    """
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: None,
    )

    result = run_maven(tmp_path, goals=("-q", "compile"))
    assert result.skipped
    assert result.passed          # skipped soft-passes
    assert "not on PATH" in result.skipped_reason


def test_run_maven_fails_without_pom(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: "/usr/bin/mvn",
    )
    result = run_maven(tmp_path, goals=("-q", "compile"))
    assert not result.passed
    assert "no pom.xml" in result.stderr


def test_verify_unit_soft_passes_without_pom(tmp_path: Path):
    """Before the translator has run, there's no pom.xml in workdir —
    verify_unit must not hard-fail in that case (the translator is
    responsible for its own failure path; this gate is about compile
    correctness of *produced* output).
    """
    assert CARTRIDGE.verify_unit(tmp_path, _item()) is True


def test_verify_unit_soft_passes_when_mvn_missing(tmp_path: Path, monkeypatch):
    """Even with a pom.xml in place, if ``mvn`` isn't on PATH the
    cartridge must soft-pass — don't punish devs without Maven.
    """
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    monkeypatch.setattr(
        "maf_generic_migrator_v1.platform_core.tools.test_runner.shutil.which",
        lambda _: None,
    )
    assert CARTRIDGE.verify_unit(tmp_path, _item()) is True


@pytest.mark.skipif(not MVN_AVAILABLE, reason="mvn not installed")
def test_run_maven_compiles_trivial_pom(tmp_path: Path):
    """When Maven is actually available, a minimal pom must compile.
    Catches broken PATH / broken mvn installs / JAVA_HOME issues.
    """
    (tmp_path / "pom.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example.test</groupId>
  <artifactId>trivial</artifactId>
  <version>0.0.1-SNAPSHOT</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
</project>
""",
        encoding="utf-8",
    )
    (tmp_path / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (tmp_path / "src" / "main" / "java" / "com" / "example" / "Hello.java").write_text(
        "package com.example;\npublic class Hello {}\n",
        encoding="utf-8",
    )
    result = run_maven(tmp_path, goals=("-q", "compile"), timeout=600)
    assert result.passed, (
        f"trivial mvn compile failed:\n"
        f"exit={result.exit_code}\n"
        f"stderr tail:\n{result.stderr[-1500:]}"
    )
