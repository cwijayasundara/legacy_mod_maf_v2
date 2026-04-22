"""End-to-end tests for the COBOL → Java translator orchestrator.

Uses ``FakeChatClient`` with a canned Maven-module response so tests are
deterministic and fast. Covers: target-style-specific prompt selection,
data-dictionary + spec rendering in the user message, output parsing to
a nested directory layout, path-traversal safety.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.cartridge import (
    CARTRIDGE,
)
from maf_generic_migrator_v1.cartridges.cobol_to_java25_springboot.translator import (
    translate_program,
)
from maf_generic_migrator_v1.platform_core.comprehension import (
    FakeChatClient,
    run_comprehension,
)
from maf_generic_migrator_v1.platform_core.kg import NetworkXStore

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "corpus"
    / "fixtures"
    / "payroll"
)
PROMPTS_DIR = (
    Path(__file__).resolve().parents[1]
    / "cartridges"
    / "cobol_to_java25_springboot"
    / "prompts"
)


CANNED_MAVEN_MODULE = """Here is the translated module:

```xml path=pom.xml
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>payroll</artifactId>
  <version>0.1.0-SNAPSHOT</version>
</project>
```

```java path=src/main/java/com/example/PAYROLL/Application.java
package com.example.PAYROLL;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class Application {
    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);
    }
}
```

```java path=src/main/java/com/example/PAYROLL/PayrollJobConfig.java
package com.example.PAYROLL;

import org.springframework.batch.core.Job;
import org.springframework.batch.core.Step;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class PayrollJobConfig {
    @Bean
    public Job payrollJob() { return null; }
}
```

```java path=src/test/java/com/example/PAYROLL/ContextLoadTest.java
package com.example.PAYROLL;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class ContextLoadTest {
    @Test
    void contextLoads() {}
}
```

```yaml path=src/main/resources/application.yml
spring:
  batch:
    job:
      enabled: false
```

```markdown path=README.md
# PAYROLL — monthly payroll batch job
```
"""


async def _prepare_comprehension():
    """Populate KG + run comprehension against PAYROLL fixture."""
    adapter = CARTRIDGE.adapters()["cobol"]
    store = NetworkXStore()
    adapter.extract_kg(FIXTURE_ROOT, FIXTURE_ROOT / "PAYROLL.cbl", store)
    CARTRIDGE.ingest_kg_extras(FIXTURE_ROOT, store)

    # Canned structured program summary so the BusinessSpec parses.
    comp_client = FakeChatClient(
        canned={
            "MAIN-SECTION": (
                "Purpose: computes monthly payroll gross including overtime bonus.\n"
                "Inputs: PROD.PAYROLL.MASTER\n"
                "Outputs: PROD.PAYROLL.DAILY\n"
                "Side effects: CALL NOTIFYLIB, EXEC SQL INSERT\n"
                "Key invariants:\n"
                "  - COMP-3 arithmetic on WS-GROSS\n"
            )
        },
        default="(mock summary)",
    )
    result = await run_comprehension(store, CARTRIDGE, chat_client=comp_client)
    return store, result


# --------------------------------------------------------------------------- #
# Target style + files written
# --------------------------------------------------------------------------- #


async def test_payroll_translates_as_batch_with_full_layout():
    store, comp = await _prepare_comprehension()
    translator_client = FakeChatClient(default=CANNED_MAVEN_MODULE)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td) / "PAYROLL"
        tresult = await translate_program(
            comp.specs[0],
            store=store,
            matrix=comp.crud_matrix,
            chat_client=translator_client,
            prompts_dir=PROMPTS_DIR,
            workdir=workdir,
        )

        assert tresult.error is None
        assert tresult.target_style == "batch"
        # Maven layout survived the fenced-block round-trip.
        expected = {
            "pom.xml",
            "src/main/java/com/example/PAYROLL/Application.java",
            "src/main/java/com/example/PAYROLL/PayrollJobConfig.java",
            "src/test/java/com/example/PAYROLL/ContextLoadTest.java",
            "src/main/resources/application.yml",
            "README.md",
        }
        assert set(tresult.files_written) == expected
        for rel in expected:
            assert (workdir / rel).is_file()


async def test_batch_system_prompt_is_used():
    """The system prompt sent to the chat client must be translator_batch.md,
    not the CICS or subroutine variants.
    """
    store, comp = await _prepare_comprehension()
    translator_client = FakeChatClient(default=CANNED_MAVEN_MODULE)

    with tempfile.TemporaryDirectory() as td:
        await translate_program(
            comp.specs[0],
            store=store,
            matrix=comp.crud_matrix,
            chat_client=translator_client,
            prompts_dir=PROMPTS_DIR,
            workdir=Path(td),
        )
        systems = [s for (s, _) in translator_client.calls]
        assert len(systems) == 1
        assert "Spring Batch" in systems[0]
        assert "@RestController" not in systems[0]


async def test_user_prompt_includes_spec_and_data_dictionary():
    store, comp = await _prepare_comprehension()
    translator_client = FakeChatClient(default=CANNED_MAVEN_MODULE)

    with tempfile.TemporaryDirectory() as td:
        await translate_program(
            comp.specs[0],
            store=store,
            matrix=comp.crud_matrix,
            chat_client=translator_client,
            prompts_dir=PROMPTS_DIR,
            workdir=Path(td),
        )
        user = translator_client.calls[0][1]

        # Business spec content
        assert "PROD.PAYROLL.MASTER" in user
        assert "PROD.PAYROLL.DAILY" in user
        assert "CALL NOTIFYLIB" in user
        assert "COMP-3" in user

        # Data dictionary — one row per field
        assert "PAYROLL-REC" in user
        assert "EMP-ID" in user
        assert "EMP-HOURS" in user
        assert "EMP-RATE" in user
        # PIC values survive into the dictionary
        assert "9(3)V99" in user

        # Per-paragraph chunks with raw COBOL
        assert "COMPUTE-GROSS" in user
        assert "MULTIPLY EMP-HOURS" in user


async def test_path_traversal_attempt_is_refused():
    """A malicious / confused LLM that emits a fenced block with
    ``path=../pwned`` must NOT be able to write outside the workdir.
    """
    store, comp = await _prepare_comprehension()
    evil = """```xml path=../pwned.xml
<evil>
```
```xml path=pom.xml
<ok/>
```
"""
    translator_client = FakeChatClient(default=evil)

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td) / "unit"
        tresult = await translate_program(
            comp.specs[0],
            store=store,
            matrix=comp.crud_matrix,
            chat_client=translator_client,
            prompts_dir=PROMPTS_DIR,
            workdir=workdir,
        )
        assert tresult.files_written == ["pom.xml"]
        assert not (Path(td) / "pwned.xml").exists()


async def test_empty_output_records_error_and_saves_raw():
    store, comp = await _prepare_comprehension()
    translator_client = FakeChatClient(default="no fenced blocks here, sorry.")

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td) / "unit"
        tresult = await translate_program(
            comp.specs[0],
            store=store,
            matrix=comp.crud_matrix,
            chat_client=translator_client,
            prompts_dir=PROMPTS_DIR,
            workdir=workdir,
        )
        assert tresult.files_written == []
        assert tresult.error is not None
        # Raw response saved for debugging.
        assert (workdir / "translator_response.md").is_file()


async def test_chat_failure_surfaces_as_result_error():
    class BoomClient:
        async def chat(self, *, system: str, user: str) -> str:
            raise RuntimeError("rate limited")

    store, comp = await _prepare_comprehension()
    with tempfile.TemporaryDirectory() as td:
        tresult = await translate_program(
            comp.specs[0],
            store=store,
            matrix=comp.crud_matrix,
            chat_client=BoomClient(),
            prompts_dir=PROMPTS_DIR,
            workdir=Path(td),
        )
        assert tresult.error is not None
        assert "rate limited" in tresult.error
        assert tresult.files_written == []


# --------------------------------------------------------------------------- #
# Per-style prompt selection (synthetic stores)
# --------------------------------------------------------------------------- #


def _minimal_store_with(kind: str, program_id: str) -> NetworkXStore:
    """Tiny hand-built store that classifies as the requested style."""
    from maf_generic_migrator_v1.platform_core.kg import KGEdge, KGNode, SourceSpan

    s = NetworkXStore()
    span = SourceSpan(file="x.cbl", start_line=1, end_line=1)
    s.add_node(KGNode(id=f"program:{program_id}", kind="program", name=program_id, span=span,
                      llm_summary="Purpose: test\nInputs: none\nOutputs: none\nSide effects: none\nKey invariants:\n"))
    s.add_node(KGNode(id=f"paragraph:{program_id}:MAIN", kind="paragraph", name="MAIN", span=span,
                      raw_text="DISPLAY 'HI'.", llm_summary="prints hi"))
    s.add_edge(KGEdge(source=f"program:{program_id}", target=f"paragraph:{program_id}:MAIN", kind="contains"))

    if kind == "batch":
        s.add_node(KGNode(id="step:J:S", kind="step", name="S", span=span))
        s.add_edge(KGEdge(source=f"program:{program_id}", target="step:J:S", kind="step_of"))
    elif kind == "cics":
        s.add_node(KGNode(id=f"txn_call:{program_id}:0:1", kind="txn_call",
                          name="CICS-LINK", span=span, attributes={"verb": "LINK"}))
        s.add_edge(KGEdge(source=f"paragraph:{program_id}:MAIN",
                          target=f"txn_call:{program_id}:0:1", kind="calls"))
    # subroutine: nothing extra
    return s


@pytest.mark.parametrize(
    "style,expected_marker",
    [
        ("batch", "Spring Batch"),
        ("cics", "@RestController"),
        ("subroutine", "@Service"),
    ],
)
async def test_each_style_loads_its_specific_prompt(style, expected_marker):
    from maf_generic_migrator_v1.platform_core.comprehension.business_spec import (
        render_business_spec,
    )

    program_id = f"P-{style.upper()}"
    store = _minimal_store_with(style, program_id)
    from maf_generic_migrator_v1.platform_core.pipeline.crud import build_crud_matrix
    matrix = build_crud_matrix(store)
    spec = render_business_spec(store, program_id, matrix=matrix)

    translator_client = FakeChatClient(default="```text path=noop.txt\nok\n```")
    with tempfile.TemporaryDirectory() as td:
        result = await translate_program(
            spec, store=store, matrix=matrix,
            chat_client=translator_client, prompts_dir=PROMPTS_DIR, workdir=Path(td),
        )
        assert result.target_style == style
        assert expected_marker in translator_client.calls[0][0]
