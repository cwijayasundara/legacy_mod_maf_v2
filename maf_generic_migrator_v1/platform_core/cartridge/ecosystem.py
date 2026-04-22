"""EcosystemSignature: the (language, version, framework, runtime, build-system) tuple
that identifies a side of a migration pair.

A cartridge declares the ``source`` and ``target`` ecosystems it handles. The platform
uses signatures for cartridge lookup and for validating that a repo matches what the
cartridge expects.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class EcosystemSignature(BaseModel):
    """Identifies one side of a migration pair."""

    language: str = Field(..., description="Primary language: 'python', 'java', 'node', 'typescript', 'csharp', 'cobol', 'polyglot', ...")
    version: str | None = Field(None, description="Language/runtime version when meaningful, e.g. '8', '22', '3.11'")
    framework: str | None = Field(None, description="Framework, e.g. 'spring-boot-2', 'angular-1.x', 'flask'")
    runtime: str | None = Field(None, description="Deployment/runtime target, e.g. 'aws-lambda', 'azure-fn', 'jvm', 'iis'")
    build_system: str | None = Field(None, description="Build tool, e.g. 'maven', 'gradle', 'npm', 'msbuild', 'setuptools'")
    extra: dict[str, str] = Field(default_factory=dict, description="Ecosystem-specific extensions (e.g. aws services in use)")

    def matches(self, other: EcosystemSignature) -> bool:
        """Return True if ``other`` is a compatible realization of ``self``.

        ``None`` fields in ``self`` are wildcards. Useful for cartridges that are
        language-specific but version-agnostic (e.g. any Java source → Java 22).
        """
        for field in ("language", "version", "framework", "runtime", "build_system"):
            expected = getattr(self, field)
            if expected is None:
                continue
            actual = getattr(other, field)
            if actual != expected:
                return False
        return True

    def short_id(self) -> str:
        """Stable short identifier, e.g. ``java-8-spring-boot-2-maven``."""
        parts = [p for p in (self.language, self.version, self.framework, self.runtime, self.build_system) if p]
        return "-".join(parts)
