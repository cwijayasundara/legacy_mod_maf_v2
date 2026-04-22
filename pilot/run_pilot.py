#!/usr/bin/env python3
"""End-to-end pilot runner for the COBOL → Java 17 / Spring Boot cartridge.

Exercises every pipeline stage on the pilot corpus (``pilot/corpus/``)
with ``FakeChatClient`` so the run is deterministic and free. Produces
structured output under ``pilot/output/`` that the pilot report consumes.

Stages:
    1. Cartridge discovery — enumerate programs via ``unit_classifier``.
    2. Adapter — build the knowledge graph per program.
    3. JCL ingest — layer job/step/dataset edges on top.
    4. Comprehension — bottom-up LLM summaries populate every node.
    5. CRUD + seams — structural analyses over the KG.
    6. Business specs — one Markdown spec per program.
    7. Classification — assign target style per program.
    8. Translation — emit a Spring Boot Maven module per program.
    9. verify_unit — mvn compile (soft-pass) + dualrun (NullRunner).

Run directly::

    python pilot/run_pilot.py

Exit code 0 on success; 1 when any stage raised an unhandled exception.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (  # noqa: E402
    CARTRIDGE,
)
from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.target_style import (  # noqa: E402
    classify_program,
)
from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.translator import (  # noqa: E402
    translate_program,
)
from maf_generic_migrator_v1.platform_core.comprehension import (  # noqa: E402
    FakeChatClient,
    run_comprehension,
)
from maf_generic_migrator_v1.platform_core.ir import BacklogItem, Contract  # noqa: E402
from maf_generic_migrator_v1.platform_core.kg import NetworkXStore  # noqa: E402
from maf_generic_migrator_v1.platform_core.pipeline.crud import build_crud_matrix  # noqa: E402
from maf_generic_migrator_v1.platform_core.pipeline.seams import rank_seams  # noqa: E402

PILOT_ROOT = Path(__file__).resolve().parent
CORPUS_ROOT = PILOT_ROOT / "corpus"
OUTPUT_ROOT = PILOT_ROOT / "output"
PROMPTS_DIR = (
    ROOT
    / "maf_generic_migrator_v1"
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "prompts"
)


# --------------------------------------------------------------------------- #
# Result bookkeeping
# --------------------------------------------------------------------------- #


@dataclass
class StageResult:
    name: str
    ok: bool = True
    duration_ms: int = 0
    note: str = ""
    error: str = ""


@dataclass
class ProgramResult:
    program_id: str
    source_file: str
    target_style: str | None = None
    files_written: list[str] = field(default_factory=list)
    verify_unit_passed: bool | None = None
    stages: list[StageResult] = field(default_factory=list)
    error: str = ""


@dataclass
class PilotSummary:
    corpus_root: str
    started_at: float
    duration_s: float = 0.0
    programs: list[ProgramResult] = field(default_factory=list)
    kg_node_counts: dict[str, int] = field(default_factory=dict)
    crud_matrix_rows: int = 0
    seam_candidates: int = 0
    fatal: str = ""

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# FakeChatClient responses — plausible enough to exercise the pipeline
# --------------------------------------------------------------------------- #


def _comprehension_client() -> FakeChatClient:
    """Canned replies keyed on tokens unique to each program's prompt.

    The summarizer's user message contains ``- name: `<id>``` — we key
    the program-level structured blocks on those markers so each
    program gets its own purpose/inputs/outputs block rather than
    falling back to the default. Paragraph-level keys use statement
    fragments that only appear in one program, keeping section
    summaries per-program distinct.
    """
    canned = {
        # Paragraph-level cues — picked so each fragment only matches
        # one program's paragraph raw_text.
        "ADD WS-OT TO WS-GROSS":              "adds overtime hours to WS-GROSS",
        "MULTIPLY EMP-HOURS":                 "computes WS-GROSS as hours times rate",
        "OPEN INPUT  CUST-MASTER":            "opens the customer master input file",
        "CLOSE CUST-MASTER":                  "closes customer master and statement output",
        "CLOSE PAYROLL-FILE":                 "closes both payroll files and stops the run",
        "EXEC CICS RECEIVE MAP('CUSTMAP')":   "receives the inbound CICS customer map",
        "EXEC CICS SEND MAP('CUSTMAP')":      "returns the customer response map",
        "EXEC CICS SEND TEXT":                "sends an error banner on the CICS screen",
        "COMPUTE WS-TEMP = LNK-BALANCE":      "multiplies LNK-BALANCE by LNK-RATE into WS-TEMP",
        "SELECT CUST_ID, CUST_FIRST":         "fetches the customer row from DB2 by ID",
        "SELECT COALESCE(SUM(TXN_AMT)":       "aggregates transaction amounts for the period",
        "INSERT INTO STATEMENT_HISTORY":      "persists one statement row to DB2 history",
        "CALL 'INTCALC'":                     "invokes the interest-calculator subroutine",
        "WRITE STMT-REC":                     "writes one statement line to the output file",
        # Program-level structured blocks — keyed on ``- name: `<id>```
        # which the summarizer emits unconditionally for every node.
        "- name: `PAYROLL`":  (
            "Purpose: monthly payroll gross calculation with overtime bonus.\n"
            "Inputs: PROD.PAYROLL.MASTER\n"
            "Outputs: PROD.PAYROLL.DAILY\n"
            "Side effects: AUDIT INSERT, CALL NOTIFYLIB\n"
            "Key invariants:\n"
            "  - WS-GROSS uses COMP-3 with two decimal digits\n"
            "  - overtime bonus fires only when WS-GROSS > 1000\n"
        ),
        "- name: `CUSTINQ`": (
            "Purpose: CICS customer inquiry — fetches customer status and name by ID.\n"
            "Inputs: CUSTOMER (DB2 table)\n"
            "Outputs: CUSTMAP (CICS response map)\n"
            "Side effects: EXEC SQL SELECT, EXEC CICS SEND MAP, EXEC CICS RETURN\n"
            "Key invariants:\n"
            "  - CUST-BAL and CUST-RATE are COMP-3 with preserved scale\n"
            "  - suspended accounts return a different map than active ones\n"
        ),
        "- name: `STMTGEN`": (
            "Purpose: monthly statement generation per customer.\n"
            "Inputs: PROD.CUST.MASTER, TRANSACTIONS\n"
            "Outputs: PROD.STMT.DAILY, STATEMENT_HISTORY\n"
            "Side effects: CALL INTCALC, EXEC SQL SELECT, EXEC SQL INSERT\n"
            "Key invariants:\n"
            "  - all monetary fields COMP-3 with 2 decimal digits\n"
            "  - INTCALC LNK-OK must be 'Y' before STATEMENT_HISTORY insert\n"
        ),
        "- name: `INTCALC`": (
            "Purpose: pure interest calculator — multiplies balance by rate.\n"
            "Inputs: none (parameters via LINKAGE SECTION)\n"
            "Outputs: none (results via LINKAGE SECTION)\n"
            "Side effects: none\n"
            "Key invariants:\n"
            "  - LNK-BALANCE must be non-negative\n"
            "  - LNK-RATE in [0, 1]\n"
            "  - COMP-3 arithmetic with no rounding\n"
        ),
    }
    return FakeChatClient(canned=canned, default="(pilot mock summary)")


_CANNED_MAVEN_MODULE_TEMPLATE = """```xml path=pom.xml
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.3.0</version>
  </parent>
  <groupId>com.example</groupId>
  <artifactId>{artifact}</artifactId>
  <version>0.1.0-SNAPSHOT</version>
  <properties>
    <java.version>17</java.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter</artifactId>
    </dependency>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-test</artifactId>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
```

```java path=src/main/java/com/example/{pkg}/Application.java
package com.example.{pkg};

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class Application {{
    public static void main(String[] args) {{
        SpringApplication.run(Application.class, args);
    }}
}}
```

```java path=src/test/java/com/example/{pkg}/ContextLoadTest.java
package com.example.{pkg};

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class ContextLoadTest {{
    @Test
    void contextLoads() {{}}
}}
```

```yaml path=src/main/resources/application.yml
spring:
  application:
    name: {artifact}
```

```markdown path=README.md
# {program_id}

Generated by the pilot runner as a scaffold — the actual business logic
from the COBOL source is NOT yet translated. This module exists to prove
the end-to-end pipeline (adapter → KG → comprehension → business spec
→ translator fence-block output) plumbs cleanly.
```
"""


def _translator_client_for(program_id: str) -> FakeChatClient:
    """Per-program canned response so the translator emits a Maven module
    whose ``artifactId`` and package name are unique per program. Keeps
    the translator output reviewable side-by-side.
    """
    pkg = program_id.upper()
    artifact = program_id.lower()
    response = _CANNED_MAVEN_MODULE_TEMPLATE.format(
        program_id=program_id, pkg=pkg, artifact=artifact
    )
    return FakeChatClient(default=response)


# --------------------------------------------------------------------------- #
# Stage helpers
# --------------------------------------------------------------------------- #


def _timed(stage_name: str, fn, *args, **kwargs) -> tuple[Any, StageResult]:
    start = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — want all failures visible
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return None, StageResult(
            name=stage_name,
            ok=False,
            duration_ms=elapsed_ms,
            error=f"{type(exc).__name__}: {exc}",
            note=traceback.format_exc(limit=3).splitlines()[-1][:200],
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return result, StageResult(name=stage_name, ok=True, duration_ms=elapsed_ms)


async def _atimed(stage_name: str, coro_fn, *args, **kwargs) -> tuple[Any, StageResult]:
    start = time.perf_counter()
    try:
        result = await coro_fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return None, StageResult(
            name=stage_name,
            ok=False,
            duration_ms=elapsed_ms,
            error=f"{type(exc).__name__}: {exc}",
            note=traceback.format_exc(limit=3).splitlines()[-1][:200],
        )
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return result, StageResult(name=stage_name, ok=True, duration_ms=elapsed_ms)


# --------------------------------------------------------------------------- #
# Pilot driver
# --------------------------------------------------------------------------- #


async def run_pilot() -> PilotSummary:
    logging.basicConfig(level=logging.INFO, format="[pilot] %(message)s")
    logger = logging.getLogger("pilot")

    summary = PilotSummary(corpus_root=str(CORPUS_ROOT), started_at=time.time())
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "business_specs").mkdir(exist_ok=True)
    (OUTPUT_ROOT / "units").mkdir(exist_ok=True)

    # 1. Discovery — let the cartridge enumerate units.
    program_files, classifier_stage = _timed(
        "unit_classifier", CARTRIDGE.unit_classifier, CORPUS_ROOT
    )
    if not classifier_stage.ok:
        summary.fatal = f"unit_classifier: {classifier_stage.error}"
        return summary
    logger.info("discovered %d programs: %s",
                len(program_files), [p.name for p in program_files])

    # 2+3. Adapter + JCL ingest → single shared KG for CRUD/seams later.
    store = NetworkXStore()
    adapter = CARTRIDGE.adapters()["cobol"]
    for path in program_files:
        _, stage = _timed(
            f"extract_kg[{path.name}]",
            adapter.extract_kg, CORPUS_ROOT, path, store,
        )
        if not stage.ok:
            logger.warning("extract_kg failed for %s: %s", path.name, stage.error)
    _, jcl_stage = _timed("ingest_kg_extras", CARTRIDGE.ingest_kg_extras, CORPUS_ROOT, store)
    if not jcl_stage.ok:
        logger.warning("ingest_kg_extras failed: %s", jcl_stage.error)

    from collections import Counter
    summary.kg_node_counts = dict(Counter(n.kind for n in store.iter_nodes()))

    # 4. Comprehension — summarize every node. The business-spec
    # renderer is classification-aware (excludes JCL touches-only
    # datasets for subroutine / cics targets), so we hand in a
    # classifier closure bound to the current store.
    def _classify(pid: str) -> str | None:
        try:
            return classify_program(store, pid)
        except KeyError:
            return None

    comp_client = _comprehension_client()
    comp_result, comp_stage = await _atimed(
        "run_comprehension",
        run_comprehension, store, CARTRIDGE, chat_client=comp_client,
        cache_path=OUTPUT_ROOT / "comprehension_cache.jsonl",
        classify=_classify,
    )
    if not comp_stage.ok:
        summary.fatal = f"run_comprehension: {comp_stage.error}"
        return summary

    # 5. CRUD + seams (diagnostics, not gating).
    matrix, _ = _timed("build_crud_matrix", build_crud_matrix, store)
    seams, _ = _timed("rank_seams", rank_seams, store, matrix)
    if matrix is not None:
        summary.crud_matrix_rows = len(matrix.rows)
        (OUTPUT_ROOT / "crud_matrix.md").write_text(
            "# Pilot CRUD matrix\n\n```\n" + matrix.render_grid() + "\n```\n",
            encoding="utf-8",
        )
    if seams is not None:
        summary.seam_candidates = len(seams)
        _write_seams_markdown(OUTPUT_ROOT / "seams.md", seams)

    # 6. Business specs.
    specs = comp_result.specs
    for spec in specs:
        (OUTPUT_ROOT / "business_specs" / f"{spec.program_id}.md").write_text(
            spec.markdown, encoding="utf-8"
        )

    # Snapshot the KG (post-comprehension) for the report.
    (OUTPUT_ROOT / "kg.json").write_text(
        store.snapshot().model_dump_json(indent=2), encoding="utf-8"
    )

    # Render the AST call graph + Fowler-style data-flow graph as DOT +
    # Mermaid (+ SVG when graphviz ``dot`` is on PATH). These are the
    # visual artifacts the CodeConcise + Uncovering-Mainframe-Seams
    # methodologies prescribe.
    from maf_generic_migrator_v1.platform_core.kg import render_graphs
    render_graphs(store, OUTPUT_ROOT / "graphs")

    # 7–9. Per program: classify → translate → verify_unit.
    spec_by_id = {s.program_id: s for s in specs}
    for path in program_files:
        source_text = path.read_text(encoding="utf-8", errors="replace")
        pid = _program_id_from_source(source_text) or path.stem.upper()
        program = ProgramResult(program_id=pid, source_file=str(path.relative_to(CORPUS_ROOT)))

        style, style_stage = _timed(f"classify[{pid}]", classify_program, store, pid)
        program.stages.append(style_stage)
        program.target_style = style

        spec = spec_by_id.get(pid)
        if spec is None:
            program.error = "no business spec rendered for this program"
            summary.programs.append(program)
            continue

        unit_workdir = OUTPUT_ROOT / "units" / pid
        unit_workdir.mkdir(parents=True, exist_ok=True)

        translator_client = _translator_client_for(pid)
        tresult, t_stage = await _atimed(
            f"translate[{pid}]",
            translate_program,
            spec,
            store=store,
            matrix=matrix,
            chat_client=translator_client,
            prompts_dir=PROMPTS_DIR,
            workdir=unit_workdir,
        )
        program.stages.append(t_stage)
        if tresult is not None:
            program.files_written = tresult.files_written
            if tresult.error:
                program.error = tresult.error

        backlog_item = BacklogItem(
            unit_id=pid, cartridge_id=CARTRIDGE.id, wave=1,
            source_paths=[str(path.relative_to(CORPUS_ROOT))], contract=Contract(),
        )
        verify_result, verify_stage = _timed(
            f"verify_unit[{pid}]",
            CARTRIDGE.verify_unit, unit_workdir, backlog_item,
        )
        program.stages.append(verify_stage)
        program.verify_unit_passed = bool(verify_result) if verify_stage.ok else False

        summary.programs.append(program)

    summary.duration_s = round(time.time() - summary.started_at, 3)
    summary.dump(OUTPUT_ROOT / "pilot_summary.json")
    logger.info("pilot complete in %.2fs — %d programs", summary.duration_s, len(summary.programs))
    return summary


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _write_seams_markdown(path: Path, seams: list) -> None:
    lines: list[str] = ["# Pilot seam ranking", ""]
    lines.append("| Rank | Resource | Readers | Writers | Score | Obs | Funnel | Asym |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for i, c in enumerate(seams, start=1):
        lines.append(
            f"| {i} | `{c.resource.display}` | {', '.join(c.readers) or '—'} | "
            f"{', '.join(c.writers) or '—'} | {c.total_score} | "
            f"{c.observable_score} | {c.funnel_score} | {c.asymmetry_score} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


import re

_PROGRAM_ID_RX = re.compile(
    r"^\s*PROGRAM-ID\s*\.\s*([A-Za-z][\w-]*)",
    re.IGNORECASE | re.MULTILINE,
)


def _program_id_from_source(text: str) -> str | None:
    m = _PROGRAM_ID_RX.search(text)
    return m.group(1).upper() if m else None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> int:
    summary = asyncio.run(run_pilot())
    if summary.fatal:
        print(f"[pilot] FATAL: {summary.fatal}", file=sys.stderr)
        return 1
    failed = [p for p in summary.programs if p.error or not p.verify_unit_passed]
    print(f"[pilot] done: {len(summary.programs)} programs, "
          f"{len(failed)} with errors or verify failures")
    return 1 if any(p.error for p in summary.programs) else 0


if __name__ == "__main__":
    sys.exit(main())
