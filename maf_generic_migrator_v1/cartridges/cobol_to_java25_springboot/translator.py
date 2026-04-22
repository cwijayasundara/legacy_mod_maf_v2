"""COBOL → Java translator orchestrator.

One entry point — ``translate_program`` — takes a populated KG store, a
CRUD matrix, a business spec, and a chat client, and produces a Maven
module under ``workdir``. The orchestrator:

1. Classifies the program's target style (``batch`` / ``cics`` /
   ``subroutine``) using ``target_style.classify_program``.
2. Loads the matching prompt from the cartridge's ``prompts/`` dir.
3. Assembles the user message with the business spec + per-paragraph
   summary+source chunks + a field data-dictionary pulled from KG
   ``field`` nodes.
4. Invokes the chat client.
5. Parses fenced-block-with-``path=`` output and writes each file under
   ``workdir``. Uses the same format the AWS cartridge already emits,
   so the parser is reused (copied here rather than cross-imported —
   the AWS unit_worker is tightly coupled to boto3 residuals).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from maf_generic_migrator_v1.platform_core.comprehension import (
    BusinessSpec,
    ChatClient,
)
from maf_generic_migrator_v1.platform_core.kg import KGNode, KGStore
from maf_generic_migrator_v1.platform_core.pipeline.crud import CRUDMatrix

from .target_style import TargetStyle, classify_program

logger = logging.getLogger(__name__)

_PROMPT_BY_STYLE: dict[TargetStyle, str] = {
    "batch": "translator_batch.md",
    "cics": "translator_cics.md",
    "subroutine": "translator_subroutine.md",
}


@dataclass
class TranslationResult:
    """What the translator produced for one program."""

    program_id: str
    target_style: TargetStyle
    files_written: list[str] = field(default_factory=list)
    raw_response: str = ""
    error: str | None = None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


async def translate_program(
    spec: BusinessSpec,
    *,
    store: KGStore,
    matrix: CRUDMatrix,
    chat_client: ChatClient,
    prompts_dir: Path,
    workdir: Path,
) -> TranslationResult:
    """Translate one COBOL program into a Spring Boot Maven module.

    ``prompts_dir`` is the cartridge's ``prompts/`` dir; the orchestrator
    loads the per-style file directly rather than going through the
    Summarizer's search-path mechanism (the translator has no fallback —
    it needs one of the three specific style prompts).
    """
    target_style = classify_program(store, spec.program_id)
    result = TranslationResult(program_id=spec.program_id, target_style=target_style)

    system_prompt = _load_style_prompt(prompts_dir, target_style)
    user_prompt = _build_user_message(spec, store, matrix)

    try:
        response = await chat_client.chat(system=system_prompt, user=user_prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("translator chat failed for %s", spec.program_id)
        result.error = f"chat failure: {exc}"
        return result

    result.raw_response = response
    workdir.mkdir(parents=True, exist_ok=True)
    result.files_written = _apply_fenced_output(response, workdir)
    if not result.files_written:
        result.error = "translator emitted no fenced file blocks"
        # Preserve the raw response for debugging regardless.
        (workdir / "translator_response.md").write_text(response, encoding="utf-8")
    return result


# --------------------------------------------------------------------------- #
# Prompt loading
# --------------------------------------------------------------------------- #


def _load_style_prompt(prompts_dir: Path, style: TargetStyle) -> str:
    filename = _PROMPT_BY_STYLE[style]
    path = prompts_dir / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"translator prompt missing for style={style!r}: {path}"
        )
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# User message assembly
# --------------------------------------------------------------------------- #


# Per-paragraph COBOL source dump has a budget so a pathological program
# (REDEFINES chains, giant PROCEDURE DIVISIONs) can't blow the context
# window. When hit, the remainder is summary-only.
_MAX_PARAGRAPH_BYTES = 4_000
_MAX_TOTAL_SOURCE_BYTES = 30_000


def _build_user_message(
    spec: BusinessSpec,
    store: KGStore,
    matrix: CRUDMatrix,
    *,
    target_style: str | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# Translate COBOL program `{spec.program_id}` to Spring Boot.\n")

    # Pre-computed Java identifiers + a style-specific deliverable list.
    # Showing only the class shapes relevant to this program's target
    # prevents the LLM from emitting a @RestController for a subroutine
    # (whose POM intentionally lacks spring-boot-starter-web).
    names = _java_names_for(spec.program_id)
    style = (target_style or "subroutine").lower()
    lines.append(
        f"## Java naming (use EXACTLY — filename MUST match public class name)\n"
        f"**Detected target style: `{style}`.** Emit only the classes listed below.\n"
    )
    # The main class and context-load test are PACKAGE-PRIVATE infrastructure
    # conventions: unprefixed is idiomatic Spring Boot, matches what the
    # reviewer's deliverable rule looks for, and is safe because the Java
    # package (``com.example.<artifactId>``) fully qualifies them.
    lines.append(
        f"- Maven ``artifactId``:   `{names['artifact_id']}`\n"
        f"- Java package:           `{names['package']}`\n"
        f"- Package directory path: `{names['package_path']}`\n"
        f"- Main application class: `Application`\n"
        f"  - File: `src/main/java/{names['package_path']}/Application.java`\n"
        f"- Context-load test:      `ContextLoadTest`\n"
        f"  - File: `src/test/java/{names['package_path']}/ContextLoadTest.java`\n"
    )
    if style == "cics":
        lines.append(
            f"- Controller class:       `{names['class_base']}Controller`\n"
            f"  - File: `src/main/java/{names['package_path']}/controller/{names['class_base']}Controller.java`\n"
            f"- Service class:          `{names['class_base']}Service`\n"
            f"  - File: `src/main/java/{names['package_path']}/service/{names['class_base']}Service.java`\n"
            "- DO NOT emit any Spring Batch ``Job`` / ``Step`` / ``Tasklet`` classes.\n"
        )
    elif style == "batch":
        lines.append(
            f"- Job-config class:       `{names['class_base']}JobConfig`\n"
            f"  - File: `src/main/java/{names['package_path']}/{names['class_base']}JobConfig.java`\n"
            "- Tasklet classes:        one per COBOL paragraph cluster doing real work.\n"
            "- DO NOT emit any ``@RestController`` classes — this is a batch job, not a REST API.\n"
            "- DO NOT pull in ``spring-boot-starter-web`` in ``pom.xml``.\n"
        )
    else:  # subroutine
        lines.append(
            f"- Service class:          `{names['class_base']}Service`\n"
            f"  - File: `src/main/java/{names['package_path']}/service/{names['class_base']}Service.java`\n"
            "- DO NOT emit any ``@RestController`` classes — this is a callable\n"
            "  library, not a REST API. Its public method signature must match\n"
            "  the COBOL ``PROCEDURE DIVISION USING`` clause 1:1.\n"
            "- DO NOT emit any Spring Batch ``Job`` / ``Step`` / ``Tasklet`` classes.\n"
            "- DO NOT pull in ``spring-boot-starter-web`` or ``-batch`` in\n"
            "  ``pom.xml`` — only ``spring-boot-starter`` plus test starters.\n"
        )
    lines.append(
        "DTO records live under ``dto/`` when the LINKAGE SECTION declares "
        "input/output records. Naming: ``<BaseName>Request`` / "
        "``<BaseName>Response`` / per-record-name DTOs.\n"
        "\n**DTOs MUST be declared as Java ``record`` types** — not classes.\n"
        "Records auto-generate a canonical all-args constructor that matches\n"
        "the field order, which removes an entire class of ``no suitable\n"
        "constructor found`` compile errors caused by callers invoking more\n"
        "arguments than a hand-written class constructor accepted. Example:\n"
        "\n```java\n"
        "public record CustomerMasterRecord(\n"
        "    String custId,\n"
        "    String custName,\n"
        "    BigDecimal balance,\n"
        "    BigDecimal rate,\n"
        "    String status\n"
        ") {}\n"
        "```\n"
        "\nEvery field the repository / mapper / service passes to the DTO\n"
        "constructor MUST appear in the ``record`` declaration, in the same\n"
        "order, with the same types. The data dictionary above gives you the\n"
        "full field list per record — use it verbatim.\n"
        "\nFor any additional class you emit, apply the same rule: the\n"
        "public class name MUST equal the filename (without ``.java``).\n"
        "\n**ONE class per concept — do NOT create twin variants.** If you\n"
        "emit a DTO named ``CustomerRec``, do NOT ALSO emit\n"
        "``CustomerRecRecord``. Pick ONE name and use it consistently\n"
        "across every file. Twin classes that differ only by a ``Record``\n"
        "suffix — ``CustomerRec`` / ``CustomerRecRecord``, ``WsLnk`` /\n"
        "``WsLnkRecord``, ``StmtRec`` / ``StmtRecRecord`` — are a\n"
        "translator bug: one file references ``Foo`` and another declares\n"
        "``FooRecord`` and ``mvn compile`` fails with\n"
        "``incompatible types: Foo cannot be converted to FooRecord``.\n"
        "\n**Sealed interfaces require BOTH sides to match.** If you\n"
        "declare ``public sealed interface X permits A, B {}``, then each\n"
        "of ``A`` and ``B`` MUST have ``implements X`` on its declaration\n"
        "(for records) or ``extends X`` (for classes). A permits clause\n"
        "without the matching implements/extends on each permitted\n"
        "subtype fails javac with ``invalid permits clause``.\n"
        "\n**ONE stub class per external CALL target — AND ZERO stubs\n"
        "for callees shown in the \"Already-migrated callee APIs\"\n"
        "section at the bottom of this message.** When the spec's side\n"
        "effects list ``CALL 'OTHERPGM'``:\n"
        "\n"
        "* If OTHERPGM appears in the \"Already-migrated callee APIs\"\n"
        "  section below, DO NOT emit any stub, interface, or duplicate\n"
        "  copy of the callee's service — just `import` its exact FQN\n"
        "  (shown there) and inject it as ``private final FooService\n"
        "  fooService;``. The real class is already on the classpath.\n"
        "* If OTHERPGM is NOT listed there (not yet migrated), emit\n"
        "  EXACTLY ONE ``@Service`` stub named ``OtherpgmService`` —\n"
        "  never multiple (``OtherpgmService`` + ``OtherpgmServiceStub``\n"
        "  + ``OtherpgmStubService`` is the ``duplicate-external-stub``\n"
        "  reviewer finding).\n"
    )

    # pom.xml dependency requirements — listed explicitly because
    # ``@NotNull`` / ``@Size`` show up on virtually every DTO we emit
    # (PIC-derived validation) but the base ``spring-boot-starter`` does
    # not pull Jakarta Validation in, leading to ``cannot find symbol``
    # compile errors. Spelling the requirement out removes the ambiguity.
    lines.append("## pom.xml must pin Java 25 LTS + Spring Boot 4.0.5")
    lines.append(
        "- ``<parent><artifactId>spring-boot-starter-parent</artifactId>\n"
        "  <version>4.0.5</version></parent>`` (or a newer 4.0.x release\n"
        "  if you know one exists — never a 3.x version).\n"
        "- ``<properties><java.version>25</java.version></properties>`` —\n"
        "  this is the ONLY Java version you may emit. DO NOT use 17,\n"
        "  21, or ``<maven.compiler.source>`` / ``.target>``. The\n"
        "  ``<java.version>`` property is what Spring Boot's parent pom\n"
        "  threads into ``maven.compiler.release`` for you.\n"
        "- The build machine runs Java 25 (LTS) and Maven 3.9.x; anything\n"
        "  older than 25 is a compile failure.\n"
        "- Spring Boot 4.0 uses Spring Framework 7 + Jakarta EE 11 —\n"
        "  keep ``jakarta.*`` imports (never ``javax.*``) and assume\n"
        "  every ``spring-boot-starter-*`` dependency version is\n"
        "  managed by the 4.0.5 parent, so you do NOT declare\n"
        "  ``<version>`` on any ``org.springframework.boot:*`` dep.\n"
    )

    lines.append("## pom.xml must include these dependencies")
    lines.append(
        "- `spring-boot-starter` (base)\n"
        "- `spring-boot-starter-validation` — MANDATORY whenever any DTO\n"
        "  in the module uses ``@NotNull`` / ``@Size`` / ``@Digits`` / any\n"
        "  ``jakarta.validation.constraints.*`` annotation. If you skip\n"
        "  this, ``mvn compile`` fails with ``package\n"
        "  jakarta.validation.constraints does not exist``.\n"
        "- `spring-boot-starter-test` (scope=test)\n"
        "- `com.h2database:h2` (scope=runtime) — if the module declares\n"
        "  any JPA / JDBC / SQL side effects, so ``@SpringBootTest``\n"
        "  ``ContextLoadTest`` boots without a real DB."
    )
    if style == "cics":
        lines.append(
            "- `spring-boot-starter-web` — this module is a REST API.\n"
            "- `spring-boot-starter-data-jpa` — when the spec's Side\n"
            "  effects list any ``EXEC SQL`` or DB2 write."
        )
    elif style == "batch":
        lines.append(
            "- `spring-boot-starter-batch` — this is a Spring Batch job.\n"
            "- `spring-boot-starter-data-jpa` — when the spec's Side\n"
            "  effects list any ``EXEC SQL`` or DB2 write.\n"
            "- NO `spring-boot-starter-web`."
        )
    else:
        lines.append(
            "- NO `spring-boot-starter-web`, NO `-batch` — library-only.\n"
            "- `spring-boot-starter-data-jpa` — only when the spec's Side\n"
            "  effects list SQL."
        )
    lines.append("")

    # Style-specific API landmines — concrete, from real compile failures.
    if style == "batch":
        lines.append(
            "## Spring Batch API — MUST use existing method signatures\n"
            "- ``org.springframework.batch.item.ExecutionContext`` does NOT have\n"
            "  a ``putBoolean(String, boolean)`` method — it has\n"
            "  ``put(String, Object)`` (auto-boxes primitives), ``putString``,\n"
            "  ``putInt``, ``putLong``, ``putDouble``. For a boolean flag use\n"
            "  ``ctx.put(\"flag\", true)`` or just a Tasklet instance field.\n"
            "- Prefer Tasklet instance fields for per-step state. Only use\n"
            "  ``ExecutionContext`` when state must survive a step boundary.\n"
            "- ``RepeatStatus.FINISHED`` is the return value of ``execute()``\n"
            "  for a one-shot Tasklet; never invent alternatives.\n"
        )
    if style == "subroutine":
        lines.append(
            "## Method signature rule (load-bearing)\n"
            "Each ``01-level`` entry in the LINKAGE SECTION becomes a SEPARATE\n"
            "parameter of the public ``@Service`` method, in ``PROCEDURE\n"
            "DIVISION USING`` order. Do NOT pack all linkage entries into one\n"
            "``Request`` DTO — the reviewer enforces a 1:1 parameter mapping\n"
            "and will reject a single-DTO signature.\n"
            "- Elementary linkage items (``PIC X(n)`` / numeric) → typed\n"
            "  parameter (``String``, ``BigDecimal``, …).\n"
            "- Group linkage items → record parameter named after the group.\n"
            "- Output-only linkage entries (written but never read) become\n"
            "  the return type (as a record when there's more than one).\n"
            "- In/out or input-only linkage entries stay as parameters.\n"
        )

    lines.append("## Business spec\n")
    lines.append(spec.markdown.strip())
    lines.append("")

    data_dict = _render_data_dictionary(store, spec.program_id)
    if data_dict:
        lines.append("## Data dictionary (PIC clauses by field)\n")
        lines.append(data_dict)
        lines.append("")

    lines.append("## Paragraph source + summaries\n")
    lines.append(_render_paragraph_chunks(store, spec.program_id))
    lines.append("")

    lines.append("## Output\n")
    lines.append(
        "Emit the full Maven module as fenced blocks with `path=` headers.\n"
        "No prose outside the fences. Every required deliverable from the\n"
        "system prompt must be present. Use the Java-naming section above\n"
        "verbatim — any filename ↔ public-class-name mismatch will fail\n"
        "``mvn compile``."
    )

    return "\n".join(lines)


def _java_names_for(program_id: str) -> dict[str, str]:
    """Derive deterministic Java identifiers from a COBOL PROGRAM-ID.

    Rules:
    * Maven artifactId: lowercase, hyphens preserved.
    * Java package: ``com.example.<artifact_id_no_hyphens>``.
    * Package dir path: the package with slashes.
    * Class base: PascalCase (first letter upper, rest lower) with
      hyphens removed. The COBOL word-segmentation is lossy — humans
      can rename later, but within one run the base is consistent.
    """
    artifact_id = program_id.lower()
    pkg_leaf = artifact_id.replace("-", "")
    package = f"com.example.{pkg_leaf}"
    package_path = package.replace(".", "/")
    # PascalCase: uppercase first character, lowercase the rest of the
    # run, rejoin across hyphens.
    class_base = "".join(chunk.capitalize() for chunk in program_id.split("-"))
    return {
        "artifact_id": artifact_id,
        "package": package,
        "package_path": package_path,
        "class_base": class_base,
    }


def _render_data_dictionary(store: KGStore, program_id: str) -> str:
    """Table of fields with PIC + the structural clauses translators
    need to get the Java output right.

    Columns:

    * ``Record`` / ``Field`` / ``Level`` / ``PIC`` — basic identity.
    * ``REDEFINES`` — when set, the field is a memory alias for the
      named sibling. Translator emits a sealed interface + records.
    * ``OCCURS`` — the clause text (``10 TIMES``, ``1 TO 100 TIMES
      DEPENDING ON X``). Translator emits ``List<T>`` plus ``@Size``
      and a ``@AssertTrue`` validator when DEPENDING ON is present.
    * ``Conditions (88-levels)`` — pipe-joined ``NAME=VALUE`` list
      the translator turns into a Java enum or predicate.
    * ``Group`` — immediate enclosing group item, when the field is
      nested beneath an intermediate group (level 10 under a level 05
      group under the 01 record).
    """
    rows: list[dict[str, str]] = []
    for node in store.iter_nodes(kind="field"):
        if not node.id.startswith(f"field:{program_id}:"):
            continue
        parts = node.id.split(":")
        record = parts[2] if len(parts) >= 3 else "?"
        attrs = node.attributes
        rows.append(
            {
                "record": record,
                "field": node.name,
                "level": attrs.get("level", "?"),
                "pic": attrs.get("pic", ""),
                "redefines": attrs.get("redefines", ""),
                "occurs": attrs.get("occurs", ""),
                "conditions": attrs.get("condition_values", ""),
                "group": attrs.get("parent_group", ""),
            }
        )

    if not rows:
        return ""

    rows.sort(key=lambda r: (r["record"], r["field"]))
    headers = ["Record", "Field", "Level", "PIC", "REDEFINES", "OCCURS", "Conditions (88)", "Group"]
    lines: list[str] = ["| " + " | ".join(headers) + " |",
                        "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        def _cell(v: str) -> str:
            return f"`{v}`" if v else "—"

        lines.append(
            f"| `{r['record']}` | `{r['field']}` | `{r['level']}` | "
            f"{_cell(r['pic'])} | {_cell(r['redefines'])} | {_cell(r['occurs'])} | "
            f"{_cell(r['conditions'])} | {_cell(r['group'])} |"
        )
    return "\n".join(lines)


def _render_paragraph_chunks(store: KGStore, program_id: str) -> str:
    """Render each paragraph's summary + raw COBOL up to a byte budget.

    Order is source-order (span.start_line ascending). When the cumulative
    budget is exhausted, remaining paragraphs are listed summary-only with
    an explicit ``<elided>`` note so the LLM knows not to invent the
    source.
    """
    paragraphs = sorted(
        (n for n in store.iter_nodes(kind="paragraph") if _belongs_to(n, program_id)),
        key=lambda n: n.span.start_line if n.span else 0,
    )
    if not paragraphs:
        return "(no paragraphs found in KG)"

    out: list[str] = []
    budget_used = 0
    for para in paragraphs:
        header = f"### Paragraph `{para.name}`"
        if para.span:
            header += f" ({para.span.file}:{para.span.start_line}-{para.span.end_line})"
        summary = para.llm_summary or "(not summarized)"

        raw = para.raw_text or ""
        para_budget = min(_MAX_PARAGRAPH_BYTES, _MAX_TOTAL_SOURCE_BYTES - budget_used)
        if para_budget <= 0:
            out.append(
                f"{header}\nSummary: {summary}\n\n`<elided — total source budget exhausted>`\n"
            )
            continue
        if len(raw) > para_budget:
            raw = raw[:para_budget] + "\n... <truncated>"
        budget_used += len(raw)

        out.append(
            f"{header}\nSummary: {summary}\n\n```cobol\n{raw}\n```\n"
        )
    return "\n".join(out)


def _belongs_to(node: KGNode, program_id: str) -> bool:
    return node.id.startswith(f"paragraph:{program_id}:")


# --------------------------------------------------------------------------- #
# Output parser
# --------------------------------------------------------------------------- #


_FENCE_WITH_PATH = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)\s+path=(?P<path>\S+)\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def _apply_fenced_output(response_text: str, workdir: Path) -> list[str]:
    """Extract fenced blocks and write each to ``workdir``.

    Path traversal guard: any block whose ``path=`` resolves outside the
    workdir is dropped and logged. Files are written with their declared
    relative path preserved (including nested ``src/main/java/...``
    packages).
    """
    # Resolve workdir once so macOS symlinks (``/var`` → ``/private/var``)
    # don't trip the traversal check.
    workdir_resolved = workdir.resolve()
    written: list[str] = []
    for match in _FENCE_WITH_PATH.finditer(response_text):
        rel = match.group("path").strip()
        body = match.group("body")
        dest = (workdir / rel).resolve()
        try:
            rel_display = dest.relative_to(workdir_resolved)
        except ValueError:
            logger.warning("refusing to write outside workdir: %s", dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written.append(str(rel_display))
    return written


__all__ = ["TranslationResult", "translate_program"]
