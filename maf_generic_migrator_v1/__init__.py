"""MAF generic migration engine, v1.

Vendor-/source-/target-agnostic legacy modernization platform built on
Microsoft Agent Framework. Migration-pair-specific behavior plugs in via
``cartridges`` (see ``maf_generic_migrator_v1/cartridges/`` for the AWS -> Azure exemplar).

Sub-packages:
  * ``platform_core`` — engine foundations (cartridge model, IR, context
    engineering, pipeline stages, MAF workflow wiring, runtime/CLI).
  * ``adapters`` — language adapters that produce the IR.
  * ``recipe_backends`` — deterministic rewrite engines (LibCST, OpenRewrite, ...).
  * ``cartridges`` — installable migration-pair plugins.
  * ``eval_harness`` — rubric-based scoring.
"""
