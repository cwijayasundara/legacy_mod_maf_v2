"""Sanity tests for the upgraded reviewer / security / tester prompts.

We don't run the LLM here — we verify the prompts load and contain the
load-bearing rule markers so a silent edit that drops them is caught.
"""
from __future__ import annotations

from pathlib import Path

import pytest

PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "prompts"
)


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "prompt_file,required_markers",
    [
        (
            "reviewer.md",
            [
                "bigdecimal-no-mathcontext",
                "primitive-float-for-numeric",
                "linkage-signature-mismatch",
                "sql-concatenated",
                "field-injection",
                "hardcoded-config",
                "undefined-class-reference",
                "undefined-method-reference",
                "broken-import",
                "spring-batch-api-mismatch",
                "query-param-type-mismatch",
                "twin-record-dto",
                "sealed-permits-without-implements",
                "duplicate-external-stub",
                '"verdict"',
                '"findings"',
            ],
        ),
        (
            "security.md",
            [
                "sql-injection",
                "credential-leak",
                "CWE-89",
                "CWE-798",
                "SecureRandom",
                '"verdict"',
                '"findings"',
            ],
        ),
        (
            "tester.md",
            [
                "BigDecimal::compareTo",
                "@WebMvcTest",
                "AssertJ",
                '"tests_added"',
            ],
        ),
    ],
)
def test_prompt_still_carries_its_rule_markers(prompt_file: str, required_markers: list[str]):
    text = _read(prompt_file)
    missing = [m for m in required_markers if m not in text]
    assert not missing, f"{prompt_file} lost rule markers: {missing}"


def test_reviewer_prompt_defines_three_verdicts():
    text = _read("reviewer.md")
    for verdict in ('"accept"', '"revise"', '"reject"'):
        assert verdict in text


def test_security_prompt_defines_pass_fail_verdict():
    text = _read("security.md")
    assert '"pass"' in text
    assert '"fail"' in text


def test_tester_prompt_excludes_equivalence_scope():
    """Dual-run owns equivalence; tester must explicitly hand that off."""
    text = _read("tester.md")
    # Phrasing isn't fixed but the boundary must be explicit.
    assert "dual-run" in text.lower()
