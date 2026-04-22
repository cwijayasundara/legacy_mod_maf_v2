"""Comprehension pipeline: LLM-enriches the knowledge graph bottom-up.

Sits between ``pipeline.grapher`` and ``pipeline.planner``. Produces two
things:

* ``llm_summary`` per KG node (used by retrieval at translation time).
* Business-spec Markdown per program (analyst-consumable artifact,
  translator's primary input).

This is the layer that makes legacy modules too big for a single prompt
tractable: translators never see raw 15K-LOC COBOL — they see a spec built
from distilled summaries.
"""
from maf_generic_migrator_v1.platform_core.comprehension.business_spec import (
    BusinessSpec,
    render_all_business_specs,
    render_business_spec,
)
from maf_generic_migrator_v1.platform_core.comprehension.pipeline import (
    ComprehensionResult,
    run_comprehension,
)
from maf_generic_migrator_v1.platform_core.comprehension.summarizer import (
    ChatClient,
    FakeChatClient,
    SummaryCache,
    Summarizer,
)

__all__ = [
    "BusinessSpec",
    "ChatClient",
    "ComprehensionResult",
    "FakeChatClient",
    "SummaryCache",
    "Summarizer",
    "render_all_business_specs",
    "render_business_spec",
    "run_comprehension",
]
