"""COBOL adapter.

Tree-sitter primary for divisions / sections / paragraphs / PERFORM /
data-division / file-description / WRITE / READ. Regex fallback for the
constructs tree-sitter-cobol mishandles today: ``CALL``, ``COPY``,
``EXEC SQL``, ``EXEC CICS``. The regex pass also runs for files the parser
rejects outright.

Emits:

* A flat ``UnitIR`` keyed on the PROGRAM-ID name (not the file path, so
  cross-program CALL edges can resolve without filename conventions).
* Optional KG structure via ``extract_kg`` — one ``program`` node rooting
  ``section`` / ``paragraph`` / ``file`` / ``record`` / ``field`` /
  ``external_call`` / ``sql_block`` / ``txn_call`` descendants.

Scope for Phase 1: node/edge extraction for the procedural + data skeleton.
Does not attempt full semantic analysis (alias resolution, 88-level folding,
or REDEFINES disambiguation) — those belong with the comprehension pipeline
and the translator's prompt context.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from maf_generic_migrator_v1.adapters._tree_sitter_utils import (
    find_nodes_by_type,
    line_of,
    node_text,
    parse,
    walk_nodes,
)
from maf_generic_migrator_v1.adapters.base import LanguageAdapter
from maf_generic_migrator_v1.platform_core.ir import Import, ServiceCall, UnitIR
from maf_generic_migrator_v1.platform_core.kg import (
    KGEdge,
    KGNode,
    KGStore,
    SourceSpan,
)

# --------------------------------------------------------------------------- #
# Regex fallbacks (tree-sitter-cobol gaps)
# --------------------------------------------------------------------------- #

_PROGRAM_ID_RX = re.compile(
    r"^\s*PROGRAM-ID\s*\.\s*([A-Za-z][\w-]*)",
    re.IGNORECASE | re.MULTILINE,
)
_COPY_RX = re.compile(
    r"^\s*COPY\s+([A-Za-z][\w-]*)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CALL_LITERAL_RX = re.compile(
    r"\bCALL\s+['\"]([A-Za-z][\w-]*)['\"]",
    re.IGNORECASE,
)
_CALL_IDENT_RX = re.compile(
    r"\bCALL\s+([A-Z][\w-]*)\b(?!\s*['\"])",
    re.IGNORECASE,
)
_EXEC_SQL_RX = re.compile(
    r"EXEC\s+SQL\s+(.*?)\s+END-EXEC",
    re.IGNORECASE | re.DOTALL,
)
_EXEC_CICS_RX = re.compile(
    r"EXEC\s+CICS\s+(.*?)\s+END-EXEC",
    re.IGNORECASE | re.DOTALL,
)
_SECTION_HEADER_RX = re.compile(
    r"^\s*([A-Za-z][\w-]*)\s+SECTION\s*\.",
    re.IGNORECASE | re.MULTILINE,
)
# SELECT <logical-name> ASSIGN TO <dd-name>. The DD name is what JCL binds
# to a real dataset via ``//<dd> DD DSN=...``.
_SELECT_ASSIGN_RX = re.compile(
    r"\bSELECT\s+([A-Za-z][\w-]*)\s+ASSIGN\s+TO\s+([A-Za-z][\w-]*)",
    re.IGNORECASE,
)
# PROCEDURE DIVISION USING <names> — the COBOL mark of a callable
# subroutine. Presence overrides ad-hoc JCL step_of edges in the
# target-style classifier.
_PROCEDURE_USING_RX = re.compile(
    r"\bPROCEDURE\s+DIVISION\s+USING\s+([A-Za-z][\w\s,-]*?)(?:\.|$)",
    re.IGNORECASE | re.MULTILINE,
)
# COPY <name>. directive — preprocessor inlines the named copybook here.
# COPY REPLACING is intentionally NOT expanded (rare + requires tokeniser).
_COPY_DIRECTIVE_RX = re.compile(
    r"^(?P<indent>\s*)COPY\s+(?P<name>[A-Za-z][\w-]*)\s*\.\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Clause parsers for WORKING-STORAGE / FILE SECTION data descriptions.
_REDEFINES_CLAUSE_RX = re.compile(r"\bREDEFINES\s+([A-Za-z][\w-]*)", re.IGNORECASE)
_OCCURS_CLAUSE_RX = re.compile(
    r"\bOCCURS\s+(\d+)(?:\s+TO\s+(\d+))?\s+TIMES(?:\s+DEPENDING\s+ON\s+([A-Za-z][\w-]*))?",
    re.IGNORECASE,
)
# 88-level condition value extraction. Handles string, numeric, and THRU ranges.
_VALUE_CLAUSE_RX = re.compile(
    r"\bVALUE(?:S)?(?:\s+IS)?\s+(.+?)\s*\.\s*$",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_PARAGRAPH_HEADER_RX = re.compile(
    r"^\s*([A-Za-z][\w-]*)\s*\.\s*$",
    re.MULTILINE,
)
_PERFORM_RX = re.compile(
    r"\bPERFORM\s+([A-Za-z][\w-]*)(?:\s+THRU\s+([A-Za-z][\w-]*))?",
    re.IGNORECASE,
)
# COBOL comment indicator in column 7 (conventional fixed-format).
_COMMENT_LINE_RX = re.compile(r"^.{6}[\*/]", re.MULTILINE)

# Scope terminators — these match the ``WORD.`` paragraph-header regex but
# are statements, not paragraph declarations. Must be filtered out.
_SCOPE_TERMINATORS = frozenset({
    "END-IF", "END-EVALUATE", "END-PERFORM",
    "END-READ", "END-WRITE", "END-REWRITE", "END-DELETE", "END-START",
    "END-CALL", "END-INVOKE", "END-RETURN",
    "END-COMPUTE", "END-ADD", "END-SUBTRACT", "END-MULTIPLY", "END-DIVIDE",
    "END-STRING", "END-UNSTRING", "END-SEARCH",
    "END-ACCEPT", "END-DISPLAY",
    "END-EXEC",
    "STOP", "EXIT", "END", "GOBACK",
})


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


@dataclass
class _ProcSpan:
    """A section or paragraph's line range within a source file."""

    kind: str                       # "section" | "paragraph"
    name: str
    start_line: int                 # 1-based, inclusive
    end_line: int                   # 1-based, inclusive
    parent_section: str | None = None


def _line_of_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _strip_comments(text: str) -> str:
    """Drop COBOL comment lines so regex fallbacks don't trip on them.

    Only handles the common fixed-format ``*`` / ``/`` indicator in column 7.
    Free-format ``*>`` comments aren't stripped; they're harmless to our
    patterns.
    """
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        if len(line) >= 7 and line[6] in ("*", "/"):
            continue
        kept.append(line)
    return "".join(kept)


def _program_id_from_source(text: str) -> str | None:
    m = _PROGRAM_ID_RX.search(text)
    return m.group(1).upper() if m else None


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


class CobolAdapter(LanguageAdapter):
    language = "cobol"
    supported_suffixes = (".cbl", ".cob", ".cobol", ".cpy")

    #: Subdirectories of the repo root to search for copybooks. Most
    #: shops use one of these conventions; overridable via the
    #: constructor when a cartridge's site layout differs.
    DEFAULT_COPYBOOK_SUBDIRS: tuple[str, ...] = ("copybooks", "copybook", "cpy", "copy", "")

    def __init__(
        self,
        *,
        copybook_subdirs: tuple[str, ...] | None = None,
    ) -> None:
        self.copybook_subdirs = copybook_subdirs or self.DEFAULT_COPYBOOK_SUBDIRS

    # -- UnitIR path ------------------------------------------------------ #

    def extract_unit(self, repo_root: Path, unit_root: Path) -> UnitIR:
        files = self.files_in_unit(unit_root)
        imports: list[Import] = []
        service_calls: list[ServiceCall] = []
        loc = 0
        byte_size = 0
        program_id: str | None = None

        for f in files:
            if f.suffix.lower() == ".cpy":
                # Copybooks aren't units; they're contributors to units that
                # COPY them. Skip in this pass.
                continue
            raw_text = f.read_text(encoding="utf-8", errors="replace")
            # COPY statements are captured as imports BEFORE expansion, so the
            # UnitIR carries the original dependency graph even when copybook
            # expansion fails (missing copybook, REPLACING clause, …).
            raw_stripped = _strip_comments(raw_text)
            for m in _COPY_RX.finditer(raw_stripped):
                imports.append(
                    Import(
                        module=m.group(1).upper(),
                        file=str(f.relative_to(repo_root)),
                        line=_line_of_offset(raw_stripped, m.start()),
                        is_external=False,
                    )
                )

            # Expand COPY directives before any other processing so
            # the rest of the extraction sees the full record layouts.
            text = self._expand_copybooks(raw_text, repo_root)
            byte_size += len(text.encode("utf-8"))
            loc += text.count("\n") + 1
            rel = str(f.relative_to(repo_root))

            stripped = _strip_comments(text)
            program_id = program_id or _program_id_from_source(stripped)

            # CALL / EXEC SQL / EXEC CICS -> ServiceCall
            service_calls.extend(self._extract_regex_service_calls(stripped, rel))

            # WRITE / READ statements -> ServiceCall (via tree-sitter when
            # available, regex when it fails).
            service_calls.extend(self._extract_file_io(text, rel))

        if program_id is None:
            # Fall back to the first source file's stem so the unit still has
            # a stable id if PROGRAM-ID wasn't found (shouldn't happen for
            # well-formed COBOL).
            program_id = unit_root.stem.upper() if unit_root.is_file() else unit_root.name.upper()

        return UnitIR(
            unit_id=program_id,
            kind="module",
            language="cobol",
            root_path=str(unit_root.relative_to(repo_root)),
            handler_entry=f"{program_id}:PROCEDURE DIVISION",
            files=[str(f.relative_to(repo_root)) for f in files],
            imports=imports,
            service_calls=service_calls,
            loc=loc,
            byte_size=byte_size,
        )

    # -- Copybook expansion --------------------------------------------- #

    def _expand_copybooks(self, text: str, repo_root: Path) -> str:
        """Inline the contents of every ``COPY <name>.`` directive.

        Looks for ``<name>.cpy`` / ``.copy`` / ``.cob`` under each
        configured subdir of ``repo_root`` (first match wins). Directives
        whose copybook isn't resolvable are left in place — the UnitIR's
        imports still record the COPY dependency, so the gap is visible
        without failing the run.

        ``COPY … REPLACING …`` is deliberately NOT expanded — REPLACING
        requires full tokenisation we haven't implemented. The directive
        is left intact and flagged via a residual-style comment.
        """

        def _resolve(name: str) -> Path | None:
            for subdir in self.copybook_subdirs:
                base = repo_root / subdir if subdir else repo_root
                for ext in (".cpy", ".copy", ".cob", ".cobol"):
                    candidate = base / f"{name}{ext}"
                    if candidate.is_file():
                        return candidate
                    # Also try upper-cased (site convention often matches the
                    # COBOL PROGRAM-ID casing, not the COPY directive's).
                    candidate_upper = base / f"{name.upper()}{ext}"
                    if candidate_upper.is_file():
                        return candidate_upper
            return None

        def _substitute(match: re.Match) -> str:
            name = match.group("name")
            path = _resolve(name)
            if path is None:
                return match.group(0)  # leave directive in place
            body = path.read_text(encoding="utf-8", errors="replace")
            # Drop any IDENTIFICATION / PROGRAM-ID / DIVISION headers in the
            # copybook — they aren't valid when inlined. Copybooks are
            # supposed to be fragments, but some sites over-copy.
            body = _strip_copybook_headers(body)
            # Prefix with the originating file so humans can trace back.
            header = f"      *> inlined from copybook {path.name}"
            return f"{header}\n{body.rstrip()}\n"

        return _COPY_DIRECTIVE_RX.sub(_substitute, text)

    # -- Service-call extraction ---------------------------------------- #

    def _extract_regex_service_calls(self, text: str, rel: str) -> list[ServiceCall]:
        out: list[ServiceCall] = []
        for m in _CALL_LITERAL_RX.finditer(text):
            out.append(
                ServiceCall(
                    service="cobol-call",
                    operation=m.group(1).upper(),
                    file=rel,
                    line=_line_of_offset(text, m.start()),
                )
            )
        for m in _EXEC_SQL_RX.finditer(text):
            out.append(
                ServiceCall(
                    service="db2-exec-sql",
                    operation=_first_word(m.group(1)),
                    file=rel,
                    line=_line_of_offset(text, m.start()),
                )
            )
        for m in _EXEC_CICS_RX.finditer(text):
            out.append(
                ServiceCall(
                    service="cics",
                    operation=_first_word(m.group(1)),
                    file=rel,
                    line=_line_of_offset(text, m.start()),
                )
            )
        return out

    def _extract_file_io(self, text: str, rel: str) -> list[ServiceCall]:
        """Detect READ / WRITE / REWRITE / OPEN statements. Tree-sitter path
        is preferred; regex catches the remainder.
        """
        out: list[ServiceCall] = []
        root, src = parse("cobol", text)
        seen: set[tuple[int, str]] = set()
        if root is not None:
            for kind in ("read_statement", "write_statement"):
                for node in find_nodes_by_type(root, {kind}):
                    op = "read" if kind == "read_statement" else "write"
                    target = _first_qualified_word(node, src) or "?"
                    key = (line_of(node), op)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(
                        ServiceCall(
                            service="vsam",
                            operation=f"{op}:{target}",
                            file=rel,
                            line=line_of(node),
                        )
                    )
        # Regex fallback for anything the tree-sitter pass missed (lines that
        # fell inside an ``ERROR`` subtree).
        for m in re.finditer(r"\b(READ|WRITE|REWRITE|DELETE)\s+([A-Za-z][\w-]*)", text, re.IGNORECASE):
            line = _line_of_offset(text, m.start())
            op = m.group(1).lower()
            key = (line, op)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                ServiceCall(
                    service="vsam",
                    operation=f"{op}:{m.group(2).upper()}",
                    file=rel,
                    line=line,
                )
            )
        return out

    # -- KG path ---------------------------------------------------------- #

    def extract_kg(self, repo_root: Path, unit_root: Path, store: KGStore) -> None:
        files = [f for f in self.files_in_unit(unit_root) if f.suffix.lower() != ".cpy"]
        if not files:
            return
        for source_file in files:
            self._extract_one_file_into_kg(repo_root, source_file, store)

    def _extract_one_file_into_kg(self, repo_root: Path, source_file: Path, store: KGStore) -> None:
        raw_text = source_file.read_text(encoding="utf-8", errors="replace")
        text = self._expand_copybooks(raw_text, repo_root)
        stripped = _strip_comments(text)
        rel = str(source_file.relative_to(repo_root))
        program_id = _program_id_from_source(stripped) or source_file.stem.upper()

        total_lines = text.count("\n") + 1
        program_id_str = program_id
        program_node_id = f"program:{program_id_str}"
        program_attrs = {"language": "cobol"}
        using_match = _PROCEDURE_USING_RX.search(stripped)
        if using_match:
            program_attrs["callable"] = "true"
            program_attrs["linkage_using"] = " ".join(
                w.strip() for w in using_match.group(1).replace(",", " ").split() if w.strip()
            )
        store.add_node(
            KGNode(
                id=program_node_id,
                kind="program",
                name=program_id_str,
                span=SourceSpan(file=rel, start_line=1, end_line=total_lines),
                attributes=program_attrs,
            )
        )

        # Prefer tree-sitter for structural nodes; fall back to regex when it
        # rejects the program entirely.
        proc_spans = self._collect_proc_spans(text)
        self._add_proc_structure(store, program_node_id, rel, text, proc_spans)
        self._add_perform_edges(store, program_node_id, rel, text, proc_spans)
        self._add_data_division(store, program_node_id, rel, text)
        self._add_regex_calls_and_exec_blocks(store, program_node_id, rel, stripped, proc_spans)
        self._add_file_io_edges(store, program_node_id, rel, text, proc_spans)

    # -- Structural: sections + paragraphs ------------------------------- #

    def _collect_proc_spans(self, text: str) -> list[_ProcSpan]:
        """Partition the procedure division into section/paragraph spans.

        Tree-sitter catches most headers; regex unions in anything trapped
        inside ``ERROR`` subtrees (common when the program contains
        ``EXEC SQL``/``EXEC CICS`` blocks the parser doesn't fully support).
        Spans extend to EOF when no further header is found, not to the
        possibly-truncated tree-sitter proc-div end.
        """
        # (line, kind, name) — deduped on (line, name) so regex & tree-sitter
        # don't double-emit the same header.
        headers_by_key: dict[tuple[int, str], tuple[int, str, str]] = {}

        root, src = parse("cobol", text)
        proc_start = 1
        eof_line = text.count("\n") + 1

        if root is not None:
            proc_divs = list(find_nodes_by_type(root, {"procedure_division"}))
            if proc_divs:
                pd = proc_divs[0]
                proc_start = line_of(pd)
                for node in walk_nodes(pd):
                    if node.type == "section_header":
                        name = _section_header_name(node, src)
                        if name:
                            key = (line_of(node), name)
                            headers_by_key[key] = (line_of(node), "section", name)
                    elif node.type == "paragraph_header":
                        name = _paragraph_header_name(node, src)
                        if name:
                            key = (line_of(node), name)
                            headers_by_key[key] = (line_of(node), "paragraph", name)

        # Regex always runs and unions with tree-sitter output — this catches
        # headers in ERROR subtrees (typical when EXEC SQL / CICS / CALL
        # appear mid-program).
        proc_div_rx_match = re.search(r"\bPROCEDURE\s+DIVISION\b", text, re.IGNORECASE)
        if proc_div_rx_match:
            proc_start = min(proc_start, _line_of_offset(text, proc_div_rx_match.start()))
        for m in _SECTION_HEADER_RX.finditer(text):
            line = _line_of_offset(text, m.start())
            if line < proc_start:
                continue
            name = m.group(1).upper()
            headers_by_key.setdefault((line, name), (line, "section", name))
        section_lines = {line for (line, _), (_, kind, _) in headers_by_key.items() if kind == "section"}
        for m in _PARAGRAPH_HEADER_RX.finditer(text):
            line = _line_of_offset(text, m.start())
            if line < proc_start or line in section_lines:
                continue
            name = m.group(1).upper()
            if name in _SCOPE_TERMINATORS:
                continue
            headers_by_key.setdefault((line, name), (line, "paragraph", name))

        headers = sorted(headers_by_key.values(), key=lambda h: h[0])
        spans: list[_ProcSpan] = []
        current_section: str | None = None
        for i, (line, kind, name) in enumerate(headers):
            next_line = headers[i + 1][0] if i + 1 < len(headers) else eof_line + 1
            end_line = max(line, next_line - 1)
            if kind == "section":
                current_section = name
                spans.append(_ProcSpan(kind="section", name=name, start_line=line, end_line=end_line))
            else:
                spans.append(
                    _ProcSpan(
                        kind="paragraph",
                        name=name,
                        start_line=line,
                        end_line=end_line,
                        parent_section=current_section,
                    )
                )
        return spans

    def _add_proc_structure(
        self,
        store: KGStore,
        program_node_id: str,
        rel: str,
        text: str,
        spans: list[_ProcSpan],
    ) -> None:
        lines = text.splitlines()
        for span in spans:
            node_id = f"{span.kind}:{_program_id_from_source(text)}:{span.name}"
            raw = "\n".join(lines[span.start_line - 1 : span.end_line])
            store.add_node(
                KGNode(
                    id=node_id,
                    kind=span.kind,
                    name=span.name,
                    span=SourceSpan(file=rel, start_line=span.start_line, end_line=span.end_line),
                    raw_text=raw,
                )
            )
            parent_id = program_node_id
            if span.kind == "paragraph" and span.parent_section:
                parent_id = f"section:{_program_id_from_source(text)}:{span.parent_section}"
            if store.has_node(parent_id):
                store.add_edge(
                    KGEdge(
                        source=parent_id,
                        target=node_id,
                        kind="contains",
                        evidence=f"{rel}:{span.start_line}",
                    )
                )

    # -- PERFORM edges ---------------------------------------------------- #

    def _add_perform_edges(
        self,
        store: KGStore,
        program_node_id: str,
        rel: str,
        text: str,
        spans: list[_ProcSpan],
    ) -> None:
        program_id = _program_id_from_source(text)
        for m in _PERFORM_RX.finditer(text):
            line = _line_of_offset(text, m.start())
            enclosing = _enclosing_paragraph(spans, line)
            if enclosing is None:
                continue
            src_id = f"paragraph:{program_id}:{enclosing.name}"
            target_name = m.group(1).upper()
            tgt_id = f"paragraph:{program_id}:{target_name}"
            if not store.has_node(src_id) or not store.has_node(tgt_id):
                continue
            store.add_edge(
                KGEdge(
                    source=src_id,
                    target=tgt_id,
                    kind="performs",
                    evidence=f"{rel}:{line}",
                )
            )

    # -- CALL / EXEC SQL / EXEC CICS ------------------------------------- #

    def _add_regex_calls_and_exec_blocks(
        self,
        store: KGStore,
        program_node_id: str,
        rel: str,
        text: str,
        spans: list[_ProcSpan],
    ) -> None:
        program_id = _program_id_from_source(text) or "UNKNOWN"
        for m in _CALL_LITERAL_RX.finditer(text):
            line = _line_of_offset(text, m.start())
            target = m.group(1).upper()
            node_id = f"external_call:{program_id}:{target}:{line}"
            store.add_node(
                KGNode(
                    id=node_id,
                    kind="external_call",
                    name=target,
                    span=SourceSpan(file=rel, start_line=line, end_line=line),
                    attributes={"target": target, "call_style": "literal"},
                )
            )
            enclosing = _enclosing_paragraph(spans, line)
            parent_id = f"paragraph:{program_id}:{enclosing.name}" if enclosing else program_node_id
            if store.has_node(parent_id):
                store.add_edge(
                    KGEdge(source=parent_id, target=node_id, kind="calls", evidence=f"{rel}:{line}")
                )

        for idx, m in enumerate(_EXEC_SQL_RX.finditer(text)):
            line = _line_of_offset(text, m.start())
            verb = _first_word(m.group(1))
            node_id = f"sql_block:{program_id}:{idx}:{line}"
            store.add_node(
                KGNode(
                    id=node_id,
                    kind="sql_block",
                    name=f"SQL-{verb}",
                    span=SourceSpan(
                        file=rel,
                        start_line=line,
                        end_line=_line_of_offset(text, m.end()),
                    ),
                    raw_text=m.group(1).strip(),
                    attributes={"verb": verb},
                )
            )
            enclosing = _enclosing_paragraph(spans, line)
            parent_id = f"paragraph:{program_id}:{enclosing.name}" if enclosing else program_node_id
            if store.has_node(parent_id):
                edge_kind = "reads" if verb in {"SELECT", "FETCH", "DECLARE"} else "writes"
                store.add_edge(
                    KGEdge(source=parent_id, target=node_id, kind=edge_kind, evidence=f"{rel}:{line}")
                )

        for idx, m in enumerate(_EXEC_CICS_RX.finditer(text)):
            line = _line_of_offset(text, m.start())
            verb = _first_word(m.group(1))
            node_id = f"txn_call:{program_id}:{idx}:{line}"
            store.add_node(
                KGNode(
                    id=node_id,
                    kind="txn_call",
                    name=f"CICS-{verb}",
                    span=SourceSpan(
                        file=rel,
                        start_line=line,
                        end_line=_line_of_offset(text, m.end()),
                    ),
                    raw_text=m.group(1).strip(),
                    attributes={"verb": verb},
                )
            )
            enclosing = _enclosing_paragraph(spans, line)
            parent_id = f"paragraph:{program_id}:{enclosing.name}" if enclosing else program_node_id
            if store.has_node(parent_id):
                store.add_edge(
                    KGEdge(source=parent_id, target=node_id, kind="calls", evidence=f"{rel}:{line}")
                )

    # -- File I/O edges (READ/WRITE -> File) ----------------------------- #

    def _add_file_io_edges(
        self,
        store: KGStore,
        program_node_id: str,
        rel: str,
        text: str,
        spans: list[_ProcSpan],
    ) -> None:
        program_id = _program_id_from_source(text) or "UNKNOWN"
        record_to_file = self._record_to_file_map(text)
        for m in re.finditer(r"\b(READ|WRITE|REWRITE|DELETE)\s+([A-Za-z][\w-]*)", text, re.IGNORECASE):
            verb = m.group(1).upper()
            target = m.group(2).upper()
            # WRITE/REWRITE take record names; map back to enclosing FD when known.
            if verb in {"WRITE", "REWRITE"}:
                target = record_to_file.get(target, target)
            file_node_id = f"file:{program_id}:{target}"
            if not store.has_node(file_node_id):
                continue
            line = _line_of_offset(text, m.start())
            enclosing = _enclosing_paragraph(spans, line)
            parent_id = f"paragraph:{program_id}:{enclosing.name}" if enclosing else program_node_id
            if not store.has_node(parent_id):
                continue
            store.add_edge(
                KGEdge(
                    source=parent_id,
                    target=file_node_id,
                    kind="reads" if verb == "READ" else "writes",
                    evidence=f"{rel}:{line} {verb}",
                )
            )

    def _record_to_file_map(self, text: str) -> dict[str, str]:
        """Build ``record-name -> FD-name`` using the tree-sitter file_section."""
        out: dict[str, str] = {}
        root, src = parse("cobol", text)
        if root is None:
            return out
        for fd in find_nodes_by_type(root, {"file_description"}):
            fd_name = _file_description_name(fd, src)
            if fd_name is None:
                continue
            for rec_list in find_nodes_by_type(fd, {"record_description_list"}):
                for entry in walk_nodes(rec_list):
                    if entry.type != "data_description":
                        continue
                    level = _first_child_text(entry, {"level_number"}, src)
                    ename = _first_child_text(entry, {"entry_name"}, src)
                    if level == "01" and ename:
                        out[ename.upper()] = fd_name
        return out

    # -- Data division ---------------------------------------------------- #

    def _add_data_division(
        self,
        store: KGStore,
        program_node_id: str,
        rel: str,
        text: str,
    ) -> None:
        """Materialize file / record / field nodes from both FILE SECTION
        and WORKING-STORAGE SECTION.

        Captures, per data_description entry:

        * ``pic`` — PIC clause (translators need scale + type)
        * ``redefines`` — name of redefined sibling, plus a ``redefines``
          edge to that sibling's node
        * ``occurs`` — raw clause text, plus ``occurs_min`` / ``occurs_max``
          / ``depending_on`` when parseable
        * ``parent_group`` — immediate enclosing group item (the last
          non-elementary entry at a lower level)
        * ``condition_values`` — pipe-joined ``<name>=<value>`` pairs for
          any 88-level children, for Java enum generation

        Level 88 entries are merged into the parent field's attributes
        rather than creating separate field nodes.
        """
        program_id = _program_id_from_source(text) or "UNKNOWN"
        assign_map: dict[str, str] = {}
        for m in _SELECT_ASSIGN_RX.finditer(text):
            assign_map[m.group(1).upper()] = m.group(2).upper()

        root, src = parse("cobol", text)
        if root is None:
            return

        # Track which records we emit so REDEFINES edges resolve correctly.
        for fd in find_nodes_by_type(root, {"file_description"}):
            fd_name = _file_description_name(fd, src)
            if fd_name is None:
                continue
            file_id = f"file:{program_id}:{fd_name}"
            file_attrs: dict[str, str] = {}
            dd_name = assign_map.get(fd_name)
            if dd_name is not None:
                file_attrs["dd_name"] = dd_name
            store.add_node(
                KGNode(
                    id=file_id,
                    kind="file",
                    name=fd_name,
                    span=SourceSpan(
                        file=rel,
                        start_line=line_of(fd),
                        end_line=fd.end_point[0] + 1,
                    ),
                    attributes=file_attrs,
                )
            )
            store.add_edge(
                KGEdge(
                    source=program_node_id, target=file_id, kind="contains",
                    evidence=f"{rel}:{line_of(fd)} FD",
                )
            )
            self._walk_record_descriptions(
                store, program_id, rel, fd, src,
                parent_id_for_records=file_id,
            )

        # Every top-level WORKING-STORAGE record becomes a record under the
        # program. Copybook-expanded content lands here too.
        for ws in find_nodes_by_type(root, {"working_storage_section"}):
            self._walk_record_descriptions(
                store, program_id, rel, ws, src,
                parent_id_for_records=program_node_id,
            )

    def _walk_record_descriptions(
        self,
        store: KGStore,
        program_id: str,
        rel: str,
        container_node,  # noqa: ANN001 — tree-sitter Node
        src: bytes,
        *,
        parent_id_for_records: str,
    ) -> None:
        """Walk data_description entries; emit record/field nodes + edges."""
        current_record: str | None = None
        # Stack of (level, field_name) for finding the current parent group
        # (the nearest enclosing entry at a lower level).
        group_stack: list[tuple[int, str]] = []

        for entry in walk_nodes(container_node):
            if entry.type != "data_description":
                continue

            level_text = _first_child_text(entry, {"level_number"}, src) or ""
            ename = _first_child_text(entry, {"entry_name"}, src)
            if not level_text or not ename:
                continue
            try:
                level_int = int(level_text)
            except ValueError:
                continue

            full_text = node_text(entry, src)
            name_upper = ename.upper()

            # Level 88 = condition name. Attach to current parent field.
            if level_int == 88:
                if group_stack:
                    parent_field_name = group_stack[-1][1]
                    self._attach_88_condition(
                        store, program_id, current_record, parent_field_name,
                        name_upper, full_text,
                    )
                continue

            # 01 starts a new record. Subsequent levels nest under it.
            if level_int == 1:
                current_record = name_upper
                rec_id = f"record:{program_id}:{current_record}"
                rec_attrs = self._parse_data_clauses(full_text)
                rec_attrs["level"] = "01"
                store.add_node(
                    KGNode(
                        id=rec_id,
                        kind="record",
                        name=current_record,
                        span=SourceSpan(
                            file=rel, start_line=line_of(entry),
                            end_line=entry.end_point[0] + 1,
                        ),
                        attributes=rec_attrs,
                    )
                )
                if store.has_node(parent_id_for_records):
                    store.add_edge(
                        KGEdge(
                            source=parent_id_for_records, target=rec_id,
                            kind="contains", evidence=f"{rel}:{line_of(entry)}",
                        )
                    )
                group_stack = [(1, current_record)]
                # REDEFINES at the record level is rare but legal.
                self._emit_redefines_edge(store, program_id, current_record, rec_attrs)
                continue

            if current_record is None:
                # Stray data_description without an enclosing 01 — skip.
                continue

            # Pop anything in the stack with level >= this level (we've
            # left those groups) before attaching.
            while group_stack and group_stack[-1][0] >= level_int:
                group_stack.pop()
            parent_group = group_stack[-1][1] if group_stack else current_record

            pic = _first_child_text(entry, {"picture_clause"}, src)
            field_attrs = self._parse_data_clauses(full_text)
            field_attrs["level"] = str(level_int).zfill(2)
            if pic:
                field_attrs["pic"] = pic.strip()
            if parent_group and parent_group != current_record:
                field_attrs["parent_group"] = parent_group

            field_id = f"field:{program_id}:{current_record}:{name_upper}"
            store.add_node(
                KGNode(
                    id=field_id,
                    kind="field",
                    name=name_upper,
                    span=SourceSpan(
                        file=rel, start_line=line_of(entry),
                        end_line=entry.end_point[0] + 1,
                    ),
                    attributes=field_attrs,
                )
            )
            # A field is contained by its immediate parent — the most
            # recent enclosing group (or the record itself if at the
            # top level of the record).
            parent_node_id = (
                f"field:{program_id}:{current_record}:{parent_group}"
                if parent_group != current_record
                else f"record:{program_id}:{current_record}"
            )
            if store.has_node(parent_node_id):
                store.add_edge(
                    KGEdge(
                        source=parent_node_id, target=field_id, kind="contains",
                        evidence=f"{rel}:{line_of(entry)}",
                    )
                )

            self._emit_redefines_edge(store, program_id, current_record, field_attrs, field_name=name_upper)
            # Push onto the stack — fields at higher levels can nest under us.
            group_stack.append((level_int, name_upper))

    # -- Clause parsing helpers ---------------------------------------- #

    def _parse_data_clauses(self, entry_text: str) -> dict[str, str]:
        """Extract REDEFINES / OCCURS clauses from a data_description's text."""
        attrs: dict[str, str] = {}
        m = _REDEFINES_CLAUSE_RX.search(entry_text)
        if m:
            attrs["redefines"] = m.group(1).upper()
        m = _OCCURS_CLAUSE_RX.search(entry_text)
        if m:
            occurs_min = m.group(1)
            occurs_max = m.group(2) or m.group(1)
            depending_on = m.group(3)
            attrs["occurs"] = (
                f"{occurs_min} TIMES" if occurs_min == occurs_max
                else f"{occurs_min} TO {occurs_max} TIMES"
            )
            if depending_on:
                attrs["occurs"] += f" DEPENDING ON {depending_on.upper()}"
            attrs["occurs_min"] = occurs_min
            attrs["occurs_max"] = occurs_max
            if depending_on:
                attrs["depending_on"] = depending_on.upper()
        return attrs

    def _emit_redefines_edge(
        self,
        store: KGStore,
        program_id: str,
        current_record: str | None,
        attrs: dict[str, str],
        *,
        field_name: str | None = None,
    ) -> None:
        """Wire the REDEFINES edge when the redefined sibling exists."""
        target = attrs.get("redefines")
        if not target or current_record is None:
            return
        # The redefining entity is either a field (when field_name given)
        # or the record itself (01-level REDEFINES another record).
        src_id = (
            f"field:{program_id}:{current_record}:{field_name}"
            if field_name
            else f"record:{program_id}:{current_record}"
        )
        # The target is a sibling at the same record (for field REDEFINES)
        # or another record (for record REDEFINES).
        candidates = [
            f"field:{program_id}:{current_record}:{target}",
            f"record:{program_id}:{target}",
        ]
        for tgt_id in candidates:
            if store.has_node(tgt_id) and store.has_node(src_id):
                store.add_edge(
                    KGEdge(
                        source=src_id, target=tgt_id, kind="redefines",
                        evidence=f"REDEFINES {target}",
                    )
                )
                return

    def _attach_88_condition(
        self,
        store: KGStore,
        program_id: str,
        current_record: str | None,
        parent_field_name: str,
        condition_name: str,
        entry_text: str,
    ) -> None:
        """Append a condition_values entry to the parent field's node.

        Stored as a pipe-delimited string so the KG schema doesn't need a
        structured list type; translators re-parse when generating enums.
        Format: ``NAME1=VALUE1|NAME2=VALUE2``.
        """
        if current_record is None:
            return
        field_id = (
            f"field:{program_id}:{current_record}:{parent_field_name}"
            if parent_field_name != current_record
            else f"record:{program_id}:{current_record}"
        )
        node = store.get_node(field_id)
        if node is None:
            return
        value = _parse_value_clause(entry_text)
        entry = f"{condition_name}={value}" if value else condition_name
        existing = node.attributes.get("condition_values", "")
        joined = f"{existing}|{entry}" if existing else entry
        new_attrs = dict(node.attributes)
        new_attrs["condition_values"] = joined
        store.update_node(field_id, attributes=new_attrs)


# --------------------------------------------------------------------------- #
# Small tree-sitter convenience shims
# --------------------------------------------------------------------------- #


def _first_word(text: str) -> str:
    m = re.match(r"\s*([A-Za-z][\w-]*)", text)
    return m.group(1).upper() if m else "?"


def _first_child_text(node, kinds: set[str], src: bytes) -> str | None:  # noqa: ANN001
    for child in walk_nodes(node):
        if child.type in kinds:
            return node_text(child, src)
    return None


def _first_qualified_word(node, src: bytes) -> str | None:  # noqa: ANN001
    for child in walk_nodes(node):
        if child.type == "qualified_word":
            return node_text(child, src).strip().upper()
    return None


def _section_header_name(node, src: bytes) -> str | None:  # noqa: ANN001
    text = node_text(node, src)
    m = _SECTION_HEADER_RX.match(text)
    return m.group(1).upper() if m else None


def _paragraph_header_name(node, src: bytes) -> str | None:  # noqa: ANN001
    text = node_text(node, src).strip()
    m = re.match(r"([A-Za-z][\w-]*)", text)
    return m.group(1).upper() if m else None


def _file_description_name(fd_node, src: bytes) -> str | None:  # noqa: ANN001
    entry = _first_child_text(fd_node, {"file_description_entry"}, src)
    if entry is None:
        return None
    m = re.match(r"\s*([A-Za-z][\w-]*)", entry)
    return m.group(1).upper() if m else None


def _parse_value_clause(entry_text: str) -> str:
    """Extract the RHS of a ``VALUE`` / ``VALUES IS`` clause.

    Token-walking rather than regex because COBOL VALUE clauses
    accept quoted strings (``'A'``), numerics (``0``), figurative
    constants (``ZERO``, ``SPACE``), and THRU ranges (``'A' THRU 'C'``).
    A regex would need half a page of alternation.
    """
    tokens = entry_text.replace(".", " ").split()
    i = 0
    while i < len(tokens):
        upper = tokens[i].upper()
        if upper in ("VALUE", "VALUES"):
            i += 1
            if i < len(tokens) and tokens[i].upper() == "IS":
                i += 1
            if i >= len(tokens):
                return ""
            first = tokens[i]
            # THRU range — preserve the full range as display value.
            if i + 2 < len(tokens) and tokens[i + 1].upper() in ("THRU", "THROUGH"):
                return f"{first} THRU {tokens[i + 2]}"
            return first
        i += 1
    return ""


def _strip_copybook_headers(body: str) -> str:
    """Remove any full-program headers accidentally left in a copybook.

    Strict copybooks contain only data-division fragments, but many
    real shops ship copybooks that start with IDENTIFICATION /
    PROGRAM-ID / DIVISION markers — inlining those as-is corrupts the
    host program. Drop lines that match those markers; keep everything
    else (comments, blank lines, actual data entries).
    """
    bad_prefixes = (
        "identification division",
        "program-id",
        "environment division",
        "data division",
        "working-storage section",
        "file section",
        "procedure division",
    )
    kept: list[str] = []
    for line in body.splitlines():
        lower = line.strip().lower()
        if any(lower.startswith(p) for p in bad_prefixes):
            continue
        kept.append(line)
    return "\n".join(kept)


def _enclosing_paragraph(spans: list[_ProcSpan], line: int) -> _ProcSpan | None:
    best: _ProcSpan | None = None
    for span in spans:
        if span.kind != "paragraph":
            continue
        if span.start_line <= line <= span.end_line:
            if best is None or span.start_line > best.start_line:
                best = span
    return best
