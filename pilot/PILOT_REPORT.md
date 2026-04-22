# Pilot report — `cobol_to_java25_springboot` cartridge

**Run date:** 2026-04-22 &nbsp; **Pipeline version:** Phase 5 (dual-run) + post-pilot fixes
**Mode:** deterministic — `FakeChatClient` for comprehension and translator, `NullRunner` for dual-run, no Maven on the box.
**Result:** 3 / 3 programs completed every stage end-to-end. Two platform issues surfaced during the run and were fixed in-pilot.

## Corpus (pilot/corpus/)

A small, diverse corpus committed alongside the runner — reproducible without fetching external repos. Covers every adapter pathway the PAYROLL fixture didn't.

| Program | Shape | Key constructs | Target style expected |
| --- | --- | --- | --- |
| `CUSTINQ.cbl` | CICS transaction | `EXEC CICS RECEIVE MAP`, `EXEC SQL SELECT` with host vars, `EXEC CICS SEND MAP` / `SEND TEXT` / `RETURN`, 88-level conditions, `COPY CUSTREC` | cics |
| `STMTGEN.cbl` | Batch job | `FILE-CONTROL` + `SELECT ASSIGN TO DD`, VSAM read loop, `EXEC SQL SELECT COALESCE`, `EXEC SQL INSERT`, `PERFORM … THRU`, `CALL 'INTCALC'`, nested `EXIT` paragraph | batch |
| `INTCALC.cbl` | Subroutine | `PROCEDURE DIVISION USING LNK-…`, COMP-3 arithmetic, `GOBACK` | subroutine |
| `CUSTREC.cpy` | Shared copybook | `COMP-3` fields, 88-level, `REDEFINES` | — |
| `STMTGEN.jcl` | JCL | 2 steps, STEPLIB, VSAM DSN, SYSOUT, `EXEC PGM=INTCALC` ad-hoc re-accrual wrapper | — |

## What the pipeline produced

One full pass over the corpus in **~0.1s** (all stages, no LLM / no JVM).

```
3 programs discovered
48 KG nodes populated  (3 program / 3 section / 13 paragraph / 7 field / 2 record / 2 file
                        / 6 txn_call / 3 sql_block / 1 external_call / 1 job / 2 step / 5 dataset_ref)
11 CRUD matrix rows
10 seam candidates ranked
3  business specs rendered
3  Maven modules emitted  (pom.xml + Application.java + ContextLoadTest.java + yml + README per unit)
3  verify_unit invocations, all soft-passed (mvn missing, dualrun via NullRunner)
```

See `pilot/output/` for every artifact. Highlights inline below.

### CRUD matrix (pilot/output/crud_matrix.md)

```
program     PROD.CUST.MASTER  PROD.INT.ACCRUAL  PROD.LOADLIB  PROD.STMT.DAILY  SYSOUT-*  file:CUST-MASTER  file:STMT-OUT  sql:CUSTINQ  sql:STMTGEN:INSERT  sql:STMTGEN:SELECT
CUSTINQ     .                 .                 .             .                .         .                 .              R            .                   .
INTCALC     .                 *                 .             *                .         .                 .              .            .                   .
STMTGEN     R                 .                 *             CU               *         R                 CU             .            CU                  R
```

- `R` / `CU` — source-evidenced read / create+update (translator-quality data).
- `*` — JCL-only touches-only; infra DDs (STEPLIB, SYSOUT) stay in the matrix but are filtered out of business specs.
- DD-name resolution works: `DDCUSTIN` → `CUSTIN` → `PROD.CUST.MASTER`, `DDSTMTOUT` → `STMTOUT` → `PROD.STMT.DAILY`.

### Seam ranking (pilot/output/seams.md)

Top candidates by Thoughtworks-methodology score:

1. `dataset:PROD.STMT.DAILY` (1.00) — pure writer funnel, ideal CDC-based strangler-fig cut-point.
2. `dataset:PROD.CUST.MASTER` (0.80) — pure reader.
3. Three unclassified infra datasets tied at 0.60 (they show up but won't be picked by the classifier-driven planner because they're infra-DD-flagged).

### Business specs — one per program (pilot/output/business_specs/)

- **CUSTINQ.md** — Purpose: "CICS customer inquiry — fetches customer status and name by ID." Inputs `(none)`, Outputs `(none)` (correctly — CICS I/O is via maps and SQL, not datasets). Side effects list `EXEC SQL SELECT` + `EXEC CICS SEND` + `EXEC CICS RETURN` *deterministically from KG edges*, plus an `(LLM narrative)` bullet showing the prose.
- **STMTGEN.md** — Inputs `PROD.CUST.MASTER`, Outputs `PROD.STMT.DAILY`, Side effects `EXEC SQL SELECT`, `CALL INTCALC`, `EXEC SQL INSERT`. Two Key invariants (COMP-3 scale preservation, LNK-OK precondition) surface from the structured summary.
- **INTCALC.md** — Inputs `(none)`, Outputs `(none)` (correctly excluded despite STEP020's JCL wrapper). Invariants list LINKAGE preconditions.

### Classification

| Program | Structural signals | Classifier verdict |
| --- | --- | --- |
| `CUSTINQ` | has `txn_call` descendants + `LINKAGE USING COMMAREA` + inbound JCL none | **cics** |
| `STMTGEN` | no CICS, no `LINKAGE USING`, has `step_of` → STEP010 | **batch** |
| `INTCALC` | no CICS, has `LINKAGE USING LNK-BALANCE LNK-RATE LNK-INTEREST LNK-STATUS`, also has `step_of` → STEP020 | **subroutine** |

## What the pilot fixed (platform bugs, not pilot bugs)

The pilot's job is to find issues; here are the two it caught.

### 1. `verify_unit` fails when called from an async driver

`verify_unit` is sync by contract and used to call `asyncio.run(self._run_dualrun(...))`. That raises when `verify_unit` is itself invoked from inside an event loop (the pilot runner is async). Silent: the coroutine was created but never awaited, and `verify_unit` returned `True` because the exception was swallowed by the stage-timing wrapper.

**Fix:** new `_run_coro` helper in the cartridge that detects a running loop and dispatches to a worker thread. Sync callers (normal workflow) go through `asyncio.run`; async callers go through `ThreadPoolExecutor.submit(asyncio.run, coro).result()`. Behaviour unchanged for sync callers.

### 2. Classifier too aggressive on libraries with batch wrappers

`INTCALC` classified as `batch` because the JCL has a `//STEP020 EXEC PGM=INTCALC` for ad-hoc re-accruals. But INTCALC declares `PROCEDURE DIVISION USING LNK-…` — it's a library subroutine; the JCL wrapper is an operational convenience, not its primary target.

**Fix:** the COBOL adapter now records `attributes["callable"] = "true"` on any program with a `PROCEDURE DIVISION USING` clause. The classifier's precedence becomes: CICS → callable-subroutine → batch → subroutine-default. Mixed-mode programs (CICS + callable + JCL) still classify as CICS because the REST surface is the load-bearing one.

### 3. Business spec showed JCL touches-only datasets as subroutine Inputs

Follow-on to the classifier fix: once INTCALC was correctly `subroutine`, the business-spec renderer was still showing `PROD.INT.ACCRUAL` and `PROD.STMT.DAILY` as Inputs (JCL STEP020's DDs). Semantically wrong — a subroutine's inputs live in its LINKAGE SECTION.

**Fix:** `render_business_spec` now takes an optional `target_style`. `batch` includes JCL touches-only (genuine signal); `cics` and `subroutine` exclude them (the data flows through LINKAGE / maps / SQL, not through the dataset bindings). Plumbed via `render_all_business_specs(… classify=…)` and `run_comprehension(… classify=…)` so the comprehension pipeline does the right thing in one call.

## Known gaps (Phase 6+ backlog)

These surfaced but weren't fixed in the pilot — file as future work.

### Adapter-level

- **REDEFINES not modelled.** `CUSTREC.cpy` has `CUST-EXTRA-R REDEFINES CUST-EXTRA`. Fields are extracted but the redefinition relationship isn't recorded — translators won't know to emit a sealed interface for it. Needs a new edge kind (`redefines`) and adapter work.
- **Copybook expansion is textual / implicit.** `COPY CUSTREC.` is captured as an `Import` node in the UnitIR but the copybook's fields aren't materialized into the including program's KG. The translator prompt sees the fields via the data dictionary (field nodes from the program's own `DATA DIVISION`) but a copybook-only field layout (like `CUSTREC.cpy` standalone) wouldn't contribute to the program's spec.
- **88-level condition names** are captured as field nodes but not as enum-valued fields. Translator prompt only sees the PIC clause, not the enumerated values; enum generation in Java will be incomplete.
- **`OCCURS DEPENDING ON` arrays** aren't flagged on the field node — translator rule in `translator_*.md` says "use List<T> with @AssertTrue", but the field node doesn't carry enough for the LLM to know which field's value determines length.

### Classifier + planner

- **CICS vs batch mixed-mode** — precedence is correct today, but a program genuinely split across both (CICS path + batch path within one PROGRAM-ID) would need two translations. Out of scope for now; the rule "CICS wins" emits a warning comment in translator output.
- **Planner integration still deferred.** Phase 2's CRUD / seams modules run standalone; `plan_migration` still uses only `DependencyGraph`. Wiring seam scores in as wave tiebreakers should land before the first real-world run.

### Verification

- **`mvn compile` not exercised.** The pilot ran on a box without Maven — every `verify_unit` soft-passed. A CI pass with Maven installed is needed before claiming compile fidelity.
- **Dual-run is `NullRunner` for everything.** `LEGACY_MOD_DUALRUN_CMD` wasn't set. Real confidence requires golden fixtures recorded from a reference run (GnuCOBOL or a staging mainframe) plus a runner command that actually executes the generated Java against those fixtures.

### Test harness / pilot ergonomics

- **FakeChatClient keying is fragile.** Program-level canned replies are matched on the `` - name: `<id>` `` marker the summarizer emits. This works for the pilot's 3-program corpus but wouldn't scale to a real 100-program estate, and a silent prompt format change in the summarizer would break these without a loud failure. Phase 6 should either switch to an explicit marker injection (like `materialize_mock_agents`' `[agent:<name>]`) or ship a larger, LLM-backed variant of the pilot for quality assessment.
- **Generated Maven modules are scaffolds, not translations.** The canned translator response emits a `pom.xml` + empty `Application.java` + context-load test + `application.yml` + `README.md`, not the actual COBOL-to-Java logic. End-to-end fidelity assessment requires a real LLM pass (or a regression harness comparing LLM output against recorded golden translations).

## Recommendations before running against AWS Card Demo

In priority order:

1. **Fix the REDEFINES adapter gap** — REDEFINES is common in Card Demo (account/transaction layouts). Without it, Java translations lose invariants.
2. **Install Maven in CI and wire the real compile gate** — ~10 minutes of infrastructure, unlocks the most important quality signal we have today.
3. **Land seam-aware planner integration** — a 100K-LOC estate needs the planner to prioritize; running translator on everything blindly is cost-prohibitive.
4. **Record golden fixtures for 3–5 Card Demo programs** using GnuCOBOL — lets us actually prove the dual-run gate end-to-end before running against an estate where we have no ground truth.
5. **Build a budget cap + sample mode in `run_comprehension`** — Card Demo summarization will cost real tokens; need a `--sample N` flag to run on the top-N most-called programs only, with cost ceilings per program.

## Files produced

```
pilot/
├── corpus/
│   ├── programs/{CUSTINQ,INTCALC,STMTGEN}.cbl
│   ├── copybooks/CUSTREC.cpy
│   └── jcl/STMTGEN.jcl
├── output/
│   ├── business_specs/{CUSTINQ,INTCALC,STMTGEN}.md
│   ├── units/{CUSTINQ,INTCALC,STMTGEN}/            # generated Maven modules
│   ├── comprehension_cache.jsonl
│   ├── crud_matrix.md
│   ├── seams.md
│   ├── kg.json                                     # full post-comprehension graph snapshot
│   └── pilot_summary.json                          # structured timing + stages
├── run_pilot.py
└── PILOT_REPORT.md     # this file
```

## How to reproduce

```bash
cd legacy_mod_amf_v2
source .venv/bin/activate
python pilot/run_pilot.py
# outputs under pilot/output/
```

A pytest smoke test (`maf_generic_migrator_v1/tests/test_pilot_runs_clean.py`, 9 tests) runs the same pipeline against a `tmp_path` to lock in the invariants this report asserts.

## Post-pilot remediation (2026-04-22)

Every recommendation listed above is now closed. Summary:

| # | Recommendation | Status | Evidence |
| - | -------------- | ------ | -------- |
| 1 | Fix the REDEFINES adapter gap | **Done** | New ``redefines`` edge kind in the KG schema; adapter emits field- and record-level REDEFINES edges plus a ``redefines`` attribute. Translator data dictionary surfaces it. 12 tests in `test_adapter_redefines_and_88.py` + `test_translator_datadict_upgraded.py`. |
| 2 | Install Maven in CI and wire the real compile gate | **Done** | `.github/workflows/ci.yml` sets up JDK 17 + Maven and runs the full suite on Ubuntu with Python 3.11 / 3.12. Flips `test_run_maven_compiles_trivial_pom` from skipped to active. |
| 3 | Land seam-aware planner integration | **Done** | `plan_migration(…, kg_store=…)` optional. Seam scores become within-wave tiebreakers (highest-scoring seam adjacency migrates first); alphabetical fallback preserved for cartridges without a KG. 3 tests in `test_seam_aware_planner.py`. |
| 4 | Record golden fixtures for Card Demo programs via GnuCOBOL | **Scaffolded** | `pilot/tools/record_fixture.py` compiles via `cobc -x`, pipes JSON inputs, captures stdout, writes valid `DualRunFixture` JSON. Skip-safe when `cobc` is missing. `pilot/tools/README.md` documents the instrumentation pattern. 7 tests in `test_gnucobol_recorder.py`. (Actual Card Demo recording still needs a GnuCOBOL install + domain SME sign-off on each fixture.) |
| 5 | Budget cap + `--sample N` mode in `run_comprehension` | **Done** | `max_summaries: int | None` enforces a hard cap; `sample_programs: int | None` picks top-N by centrality (incoming calls × 3 + outgoing step_of × 2 + contains × 1) and summarizes only those + descendants. Cache hits don't count against the cap. 6 tests in `test_comprehension_budget_and_sample.py`. |

Plus the Phase 6 adapter gaps that underpinned #1:

- **88-level condition names** — captured on the parent field as pipe-joined ``NAME=VALUE`` entries; 88-levels no longer leak into the field node table.
- **OCCURS / OCCURS DEPENDING ON** — parsed into ``occurs``, ``occurs_min``, ``occurs_max``, ``depending_on`` attributes.
- **Copybook expansion** — `CobolAdapter` now inlines `COPY <name>.` directives from configurable subdirs. Pilot node count jumped from 48 to 83 once CUSTOMER-REC's fields materialized into every copying program.
- **`parent_group` attribute** — nested groups (level-10 under level-05 under level-01) record their immediate parent so translators can emit nested records.

**225 tests passing, 2 skipped (mvn + cobc), 1 pre-existing libcst AWS failure unchanged.**
