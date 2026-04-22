"""Pipeline stages: discovery → grapher → planner → verifier.

Each stage is cartridge-agnostic and operates on IR produced by adapters.
"""
from maf_generic_migrator_v1.platform_core.pipeline.discovery import discover
from maf_generic_migrator_v1.platform_core.pipeline.grapher import build_graph
from maf_generic_migrator_v1.platform_core.pipeline.planner import (
    CycleError,
    dump_graph_dot,
    dump_graph_mermaid,
    plan_migration,
)
from maf_generic_migrator_v1.platform_core.pipeline.verifier import (
    DEFAULT_PROBES,
    EmulatorProbe,
    probe_all,
    probe_emulator,
    wave_verification,
)

__all__ = [
    "discover",
    "build_graph",
    "CycleError",
    "dump_graph_dot",
    "dump_graph_mermaid",
    "plan_migration",
    "DEFAULT_PROBES",
    "EmulatorProbe",
    "probe_all",
    "probe_emulator",
    "wave_verification",
]
