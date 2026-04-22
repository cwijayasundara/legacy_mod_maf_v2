"""Microsoft Agent Framework workflow wiring.

The main workflow is a 6-stage pipeline: discovery -> graph -> plan -> per-wave
fanout -> verify -> aggregate. Each stage is a MAF executor. Per-unit work
happens inside ``unit_worker`` which is itself a composite of translator /
reviewer / tester / security MAF agents.

This module intentionally does NOT import ``agent-framework`` at module load time
so that platform_core can be imported in environments without MAF installed
(e.g. unit tests). MAF imports happen lazily inside ``build_workflow()``.
"""
