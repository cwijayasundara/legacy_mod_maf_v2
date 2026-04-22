"""End-to-end comprehension test on the PAYROLL fixture.

Exercises the full Phase-3 stack: COBOL adapter → KG → JCL → Summarizer
(cartridge-prompts → platform defaults) → CRUD → BusinessSpec. Uses
``FakeChatClient`` so the test is deterministic and fast.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.platform_core.comprehension import (
    FakeChatClient,
    run_comprehension,
)
from maf_generic_migrator_v1.platform_core.kg import NetworkXStore

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "corpus"
    / "fixtures"
    / "payroll"
)


def _populated_store() -> NetworkXStore:
    s = NetworkXStore()
    adapter = CARTRIDGE.adapters()["cobol"]
    adapter.extract_kg(FIXTURE_ROOT, FIXTURE_ROOT / "PAYROLL.cbl", s)
    CARTRIDGE.ingest_kg_extras(FIXTURE_ROOT, s)
    return s


def _canned_client() -> FakeChatClient:
    """Canned replies keyed by unique source-level tokens in the user
    prompt (raw COBOL lines). The program-level reply is the structured
    block the business-spec renderer parses.
    """
    canned = {
        "ADD 50 TO WS-GROSS": "adds a 50-unit overtime bonus to WS-GROSS",
        "MULTIPLY EMP-HOURS BY EMP-RATE": "computes WS-GROSS as EMP-HOURS * EMP-RATE in COMP-3",
        "WRITE OUTPUT-REC": "writes the output row and triggers the audit SQL",
        "READ PAYROLL-FILE": "reads the next input record, dispatching to CLOSE-FILES at EOF",
        "OPEN INPUT PAYROLL-FILE": "opens the input and output files",
        "CLOSE PAYROLL-FILE": "closes both files and stops the run",
        # Section-level cue: match on child-summary phrasing
        "overtime": "orchestrates the read/compute/write loop with overtime handling",
        # Program-level: longest canned key wins, so "MAIN-SECTION" (15) beats
        # the other short section tokens.
        "MAIN-SECTION": (
            "Purpose: computes monthly payroll gross including an overtime bonus.\n"
            "Inputs: PROD.PAYROLL.MASTER\n"
            "Outputs: PROD.PAYROLL.DAILY\n"
            "Side effects: AUDIT INSERT, CALL NOTIFYLIB\n"
            "Key invariants:\n"
            "  - WS-GROSS uses COMP-3 with two decimal digits\n"
            "  - overtime bonus fires only when WS-GROSS > 1000\n"
        ),
    }
    return FakeChatClient(canned=canned, default="(fallback summary)")


async def test_every_kg_node_gets_a_summary():
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())

    # The summarizer returned at least one fresh call per node — exact count
    # is fragile but >0 and every node has a summary is the stable invariant.
    assert result.summarized_nodes > 0
    for node in store.iter_nodes():
        assert node.llm_summary is not None, f"{node.id} missing summary"


async def test_business_spec_emitted_for_program():
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())
    assert len(result.specs) == 1
    spec = result.specs[0]
    assert spec.program_id == "PAYROLL"


async def test_spec_purpose_drawn_from_structured_program_summary():
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())
    spec = result.specs[0]
    assert "monthly payroll" in spec.purpose.lower()


async def test_spec_inputs_and_outputs_match_crud_matrix():
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())
    spec = result.specs[0]

    # Inputs: the PAYIN dataset, no infrastructure DDs.
    assert spec.dataset_inputs() == ["PROD.PAYROLL.MASTER"]
    # Outputs: the daily file.
    assert spec.dataset_outputs() == ["PROD.PAYROLL.DAILY"]


async def test_spec_side_effects_enumerate_call_and_sql_sites():
    """Deterministic side-effect enumeration — must catch CALL NOTIFYLIB
    and EXEC SQL INSERT even though they're linked by ``calls``/``writes``
    edges rather than ``contains``.
    """
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())
    spec = result.specs[0]
    assert "CALL NOTIFYLIB" in spec.side_effects
    assert "EXEC SQL INSERT" in spec.side_effects


async def test_spec_invariants_parsed_from_bulleted_list():
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())
    spec = result.specs[0]
    assert any("COMP-3" in inv for inv in spec.key_invariants)
    assert any("overtime" in inv.lower() for inv in spec.key_invariants)


async def test_markdown_spec_contains_every_paragraph():
    store = _populated_store()
    result = await run_comprehension(store, CARTRIDGE, chat_client=_canned_client())
    spec = result.specs[0]
    for para_name in (
        "OPEN-FILES",
        "PROCESS-RECORDS",
        "COMPUTE-GROSS",
        "OVERTIME-BONUS",
        "EMIT-ROW",
        "CLOSE-FILES",
    ):
        assert f"### {para_name}" in spec.markdown


async def test_cartridge_prompts_used():
    """Verify the cartridge's comprehension prompts (not platform defaults)
    drove the summarizer by checking the system prompt content reached the
    chat client.
    """
    store = _populated_store()
    client = _canned_client()
    await run_comprehension(store, CARTRIDGE, chat_client=client)

    # COBOL-specific prompt text markers we added to the cartridge overrides.
    cobol_markers = [
        "COBOL paragraph summarizer",
        "COBOL section summarizer",
        "COBOL program summarizer",
    ]
    seen_systems = " ".join(sys_ for (sys_, _) in client.calls)
    for marker in cobol_markers:
        assert marker in seen_systems, f"cartridge prompt marker not used: {marker!r}"


async def test_cache_makes_rerun_cheap(tmp_path: Path):
    cache_file = tmp_path / "summaries.jsonl"
    store = _populated_store()

    client1 = _canned_client()
    await run_comprehension(store, CARTRIDGE, chat_client=client1, cache_path=cache_file)
    first_calls = len(client1.calls)
    assert first_calls > 0
    assert cache_file.is_file()

    # Re-run with a blank store; the cache should satisfy every node.
    fresh_store = _populated_store()
    client2 = _canned_client()
    result2 = await run_comprehension(
        fresh_store, CARTRIDGE, chat_client=client2, cache_path=cache_file
    )
    assert len(client2.calls) == 0
    assert result2.summarized_nodes == 0
    # But the specs still populate from the cached summaries.
    assert result2.specs[0].dataset_inputs() == ["PROD.PAYROLL.MASTER"]
