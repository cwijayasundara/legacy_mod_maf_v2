"""Unit tests for context-engineering primitives."""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.context.chunker import chunk_file, needs_chunking
from maf_generic_migrator_v1.platform_core.context.compressor import compress_history
from maf_generic_migrator_v1.platform_core.context.token_estimator import estimate_tokens, fits_in_context


def test_chunker_splits_python_at_function_boundaries() -> None:
    src = "\n".join([f"def f_{i}():\n    return {i}\n" for i in range(200)])
    chunks = chunk_file(src, "python", target_chunk_lines=100, overlap_lines=5)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.content.lstrip().startswith("def ") or "def " in c.content


def test_needs_chunking_threshold() -> None:
    assert not needs_chunking("print(1)\n")
    big = "x = 1\n" * 4000
    assert needs_chunking(big)


def test_compressor_keeps_recent_chunks_full() -> None:
    chunks = [f"chunk-{i}\n" * 20 for i in range(10)]
    result = compress_history(chunks, keep_full=3, compression_ratio=0.3)
    assert len(result.full_chunks) == 3
    assert result.total_compressed_tokens <= result.total_original_tokens


def test_token_estimator_monotone() -> None:
    short = "print(1)"
    long_ = "print(1)\n" * 1000
    assert estimate_tokens(long_) > estimate_tokens(short)
    assert fits_in_context(short)
