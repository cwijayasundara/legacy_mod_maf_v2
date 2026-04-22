"""Context engineering: chunking, compression, token budgeting, complexity scoring.

Pure, deterministic, LLM-free. Used by the planner to fit source into context
windows and prioritize work by migration difficulty.
"""
from maf_generic_migrator_v1.platform_core.context.chunker import Chunk, chunk_file, needs_chunking
from maf_generic_migrator_v1.platform_core.context.complexity_scorer import (
    ComplexityResult,
    Level,
    score_file,
    score_inventory,
)
from maf_generic_migrator_v1.platform_core.context.compressor import CompressedContext, compress_history
from maf_generic_migrator_v1.platform_core.context.token_estimator import (
    DEFAULT_PROFILE,
    TokenProfile,
    estimate_output_tokens,
    estimate_tokens,
    fits_in_context,
    token_budget_remaining,
)

__all__ = [
    "Chunk",
    "chunk_file",
    "needs_chunking",
    "ComplexityResult",
    "Level",
    "score_file",
    "score_inventory",
    "CompressedContext",
    "compress_history",
    "DEFAULT_PROFILE",
    "TokenProfile",
    "estimate_output_tokens",
    "estimate_tokens",
    "fits_in_context",
    "token_budget_remaining",
]
