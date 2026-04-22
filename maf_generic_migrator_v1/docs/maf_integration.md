# Microsoft Agent Framework integration

## Status

Platform core is **import-clean without MAF**. You can run `pytest`, discovery,
graph building, planning, and the sync reference pipeline without installing
`agent-framework`.

The `build_workflow()` function in `maf_generic_migrator_v1/platform_core/maf_workflows/main_workflow.py`
builds the durable MAF workflow. The concrete wiring is pinned to a specific
`agent-framework` version; update when pinning.

## Stage-to-executor mapping

```
discovery_executor   -- run_discovery_stage() + cartridge.adapters()
graph_executor       -- build_graph()
planner_executor     -- plan_migration()
wave_dispatch        -- fanout over PlatformBacklog.waves[i]
unit_worker          -- composite: recipes -> translator -> reviewer -> tester -> security
verify_executor      -- cartridge.verify_wave()
aggregate_executor   -- collect results into MigrationCheckpoint
```

## Checkpointing

`MigrationCheckpoint` (see `maf_workflows/checkpoints.py`) is the durable
state. The MAF workflow persists one after every stage boundary and every
wave boundary. Resuming a run means loading the last checkpoint and
continuing from its `stage`.

## Agents

Translator / reviewer / tester / security agents are materialized from
`AgentSpec` entries via `_materialize_agents()`. Each agent gets:
- Its cartridge-provided system prompt
- The cartridge-declared tool list (mapped to platform tools + cartridge tools)
- Fresh context per invocation (no parent history)

Agents receive the curated `BacklogItem` bundle as context; reaching for
`grep`/`read_file` outside `context_paths` is logged as a discovery gap.

## Budget enforcement

MAF runs the workflow under a `max_budget_usd` cap. When reached, the
workflow halts and emits a `budget_exceeded` terminal state in the
checkpoint.

## Version compatibility matrix

| agent-framework | Status |
| --------------- | ------ |
| 0.3.x           | target (initial) |
| 0.4+            | re-validate `build_workflow()` wiring |
