# Architecture

## One-sentence summary

The platform is an **invariant six-stage MAF workflow** (discovery → graph →
plan → per-wave fanout → verify → aggregate) parameterized by a pluggable
**cartridge** that encodes source/target specifics (adapters, recipes,
prompts, service maps, rubrics, corpus).

## Stage-by-stage

1. **Discovery** (`maf_generic_migrator_v1/platform_core/pipeline/discovery.py`) — walks the repo using
   the cartridge's `unit_classifier` to enumerate unit roots, then delegates
   to the cartridge's language adapters to produce a `UnitIR` per unit.
2. **Dependency graph** (`maf_generic_migrator_v1/platform_core/pipeline/grapher.py`) — combines adapter
   evidence (imports, SDK call sites) with cartridge hints (IaC ingestion,
   serverless/SAM/CDK parsing) to produce the cross-unit `DependencyGraph`.
   Ambiguous edges are flagged for optional LLM resolution.
3. **Plan** (`maf_generic_migrator_v1/platform_core/pipeline/planner.py`) — strongly-connected-component
   clustering for bounded contexts, then Kahn-style topo layering on the
   cluster DAG to produce strangler-fig waves. Scores each unit via the
   cartridge's complexity patterns; emits a `PlatformBacklog`.
4. **Per-wave fanout** (`maf_generic_migrator_v1/platform_core/maf_workflows/main_workflow.py` +
   `unit_worker.py`) — dispatches one unit-worker per backlog item in parallel
   (bounded by `max_parallel_units`). Each unit-worker applies recipes first
   (deterministic, no LLM), then invokes the translator / reviewer / tester /
   security agents on the residual.
5. **Verify** — runs `cartridge.verify_wave` before moving to the next wave.
   Red = stop; green = next wave.
6. **Aggregate** — collates per-wave results into a `MigrationCheckpoint`.

## Why MAF

MAF gives us durable workflows, checkpointing, typed executor edges,
fan-out/fan-in primitives, and OTel integration. Platform core does not
import MAF directly: the `run_sync` reference runs the exact same stages
synchronously so the platform is usable (and testable) without an MAF
runtime. The MAF workflow is built in `build_workflow()` when the
agent-framework package is pinned to a specific version.

## Cartridge contract

Each cartridge subclasses `MigrationCartridge` and exposes:

- `source` / `target` ecosystem signatures
- `adapters()` — per-source-language IR extractors
- `unit_classifier(repo_root)` — what counts as a unit
- `grapher_hints(inventory)` — IaC/manifest-seeded cross-unit edges
- `complexity_patterns(language)` — weighted regex for complexity scoring
- `translator_agents()` / `reviewer_agents()` / `tester_agents()` / `security_agents()`
- `recipes()` — deterministic rewrites (OpenRewrite / LibCST / jscodeshift / ...)
- `service_map()` / `idiom_map()` / `test_framework_map()`
- `rubrics()` + `corpus_dir()`
- `verify_unit(...)` / `verify_wave(...)`

## Determinism-first discipline

Every unit runs deterministic recipes *first*. The LLM translator sees only
the residual regions the recipes couldn't resolve. This keeps token cost and
non-determinism proportional to the intrinsic complexity of each unit — not
to codebase size.

## Directory layout

```
maf_generic_migrator_v1/platform_core/cartridge/    # cartridge plugin model (MigrationCartridge ABC, EcosystemSignature, registry)
maf_generic_migrator_v1/platform_core/ir/           # language-agnostic intermediate representation
maf_generic_migrator_v1/platform_core/context/      # context engineering: chunker, compressor, token estimator, complexity scorer
maf_generic_migrator_v1/platform_core/pipeline/     # discovery → grapher → planner → verifier
maf_generic_migrator_v1/platform_core/runtime/      # CLI entry point + .env loader
maf_generic_migrator_v1/platform_core/maf_workflows/ # MAF-facing wiring (kept lazy-imported)
maf_generic_migrator_v1/platform_core/tools/       # file/patch/test tools exposed to agents
maf_generic_migrator_v1/adapters/                   # language -> IR normalizers
maf_generic_migrator_v1/recipe_backends/            # OpenRewrite / LibCST / jscodeshift / ...
maf_generic_migrator_v1/cartridges/                 # one directory per migration pair
maf_generic_migrator_v1/eval_harness/               # rubric scoring
maf_generic_migrator_v1/tests/        # conformance + smoke + integration
maf_generic_migrator_v1/examples/     # demo fixtures + live-migration script
maf_generic_migrator_v1/docs/         # architecture + cartridge authoring + MAF integration
```

## Adding a new cartridge

See `docs/cartridge_authoring.md`.

## MAF integration notes

See `docs/maf_integration.md`.
