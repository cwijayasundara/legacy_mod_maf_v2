# Pilot tools

Helpers for recording golden dual-run fixtures from a reference COBOL
runtime. The standard pattern is to run the reference implementation
(mainframe, GnuCOBOL, or a staging environment) against a set of
representative inputs, capture the outputs, and commit them as
`pilot/corpus/fixtures/<program>/dualrun/*.json`.

## `record_fixture.py` — GnuCOBOL recorder

Compiles a COBOL program with `cobc -x`, runs it with fixture inputs on
stdin, captures outputs from stdout, and writes a DualRunFixture JSON.

### Prerequisites

- GnuCOBOL installed (`brew install gnu-cobol` on macOS, `apt-get install gnucobol` on Debian/Ubuntu).
- Your COBOL program instrumented to read inputs as `KEY=VALUE` lines
  on stdin and emit outputs as `KEY=VALUE` lines on stdout.

### Instrumenting a program for recording

Real mainframe programs read from VSAM / DB2 / CICS maps, not stdin.
Recording requires a small driver wrapper. Recommended pattern:

1. Copy the program to `programs/<name>_record.cbl`.
2. Replace the I/O entry points (`OPEN`, `READ`, `EXEC SQL`, `EXEC CICS RECEIVE`)
   with `ACCEPT <field>` statements pulling from stdin.
3. Replace outputs (`WRITE`, `EXEC SQL INSERT`, `EXEC CICS SEND`) with
   `DISPLAY "<field>=" <field>` lines.
4. Keep the business logic identical.

For a pure subroutine like `INTCALC`, this is straightforward — the
LINKAGE SECTION becomes `ACCEPT` calls and a main entrypoint that
returns via `DISPLAY`.

### Example: record a fixture for INTCALC

```bash
python pilot/tools/record_fixture.py \
    --program pilot/corpus/programs/INTCALC_record.cbl \
    --name happy-path-positive-balance \
    --target-style subroutine \
    --program-id INTCALC \
    --input '{"LNK-BALANCE": "1000.00", "LNK-RATE": "0.05"}' \
    --tolerances '{"LNK-INTEREST": {"kind": "picture", "pic": "S9(7)V99"}, "LNK-OK": {"kind": "string"}}' \
    --out pilot/corpus/fixtures/intcalc/dualrun/
```

Produces `pilot/corpus/fixtures/intcalc/dualrun/happy-path-positive-balance.json`.

### Running the fixture against the generated Java

Once the translator has emitted a Java module, set
`LEGACY_MOD_DUALRUN_CMD` to invoke the Java app with JSON on stdin:

```bash
export LEGACY_MOD_DUALRUN_CMD="java -jar pilot/output/units/INTCALC/target/intcalc.jar"
pytest maf_generic_migrator_v1/tests/test_pilot_runs_clean.py
```

The dualrun harness will pipe each fixture's inputs to the Java app,
capture the outputs, and diff them against the recorded expected
values using COBOL-aware tolerances.

## When there's no reference COBOL runtime

If you're starting cold on an estate without GnuCOBOL or mainframe
access, the practical path is:

1. Run the mainframe program against a small production input sample
   and capture the outputs via the mainframe's own logging /
   file-export mechanism.
2. Hand-craft the `expected_outputs` JSON from those captures.
3. Have a domain SME sign off on each fixture before committing.

This is slower but matches how most real modernization programs work —
direct recording via GnuCOBOL is only viable for programs with no
CICS / DB2 / VSAM dependencies (rare outside of pure algorithmic
subroutines).
