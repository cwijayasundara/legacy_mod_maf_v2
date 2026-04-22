#!/usr/bin/env python3
"""Record a dual-run fixture from a COBOL reference run via GnuCOBOL.

Workflow:

1. Compile the COBOL program with ``cobc -x`` (produces an executable).
2. Execute it once per input record, piping the inputs as JSON on stdin.
3. Capture stdout, which the COBOL program is expected to emit as JSON.
4. Wrap the (inputs, outputs) pair into a ``DualRunFixture`` and write
   it as ``<fixture-dir>/<name>.json``.

This script is **scaffolding** — it assumes:

* ``cobc`` (GnuCOBOL) is on ``PATH``.
* The COBOL program reads inputs from stdin as newline-delimited
  key=value pairs (or a custom reader you wire in) and emits outputs
  on stdout as key=value pairs.
* Real programs usually read from VSAM / SQL, not stdin — you'll need
  a small driver wrapper when recording. See `pilot/tools/README.md`
  for the recommended pattern.

Invocation::

    python pilot/tools/record_fixture.py \\
        --program pilot/corpus/programs/INTCALC.cbl \\
        --name happy-path \\
        --target-style subroutine \\
        --program-id INTCALC \\
        --input '{"LNK-BALANCE": "1000.00", "LNK-RATE": "0.05"}' \\
        --out pilot/corpus/fixtures/intcalc/dualrun/

Exit codes:

* 0 — fixture written.
* 1 — ``cobc`` not on PATH or compile failed.
* 2 — runtime failure (COBOL program exited non-zero).
* 3 — stdout was not parseable as ``key=value`` pairs.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_KEY_VALUE_RX = re.compile(r"^\s*([A-Za-z][\w-]*)\s*=\s*(.+?)\s*$")


def _compile(source: Path, out_dir: Path) -> Path | None:
    """Compile ``source`` with ``cobc -x``. Returns the executable path."""
    binary = out_dir / source.stem
    proc = subprocess.run(
        ["cobc", "-x", "-o", str(binary), str(source)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[record] cobc compile failed:\n{proc.stderr}\n"
        )
        return None
    return binary


def _run(binary: Path, inputs: dict) -> tuple[int, str, str]:
    """Execute the binary with ``inputs`` rendered as lines on stdin."""
    payload = "\n".join(f"{k}={v}" for k, v in inputs.items()) + "\n"
    proc = subprocess.run(
        [str(binary)],
        input=payload, capture_output=True, text=True, check=False,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_key_value_output(stdout: str) -> dict | None:
    """Parse ``key=value`` lines from stdout. Returns None if unparseable."""
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        m = _KEY_VALUE_RX.match(line)
        if not m:
            return None
        out[m.group(1)] = m.group(2)
    return out


def _emit_fixture(
    name: str,
    target_style: str,
    program_id: str,
    inputs: dict,
    expected: dict,
    dest_dir: Path,
    tolerances: dict | None = None,
) -> Path:
    """Write the fixture JSON in the schema ``platform_core.dualrun`` expects."""
    fixture = {
        "name": name,
        "target_style": target_style,
        "program_id": program_id,
        "inputs": inputs,
        "expected_outputs": expected,
        "tolerances": tolerances or {},
        "metadata": {
            "captured_by": "pilot/tools/record_fixture.py",
            "captured_from": "GnuCOBOL",
        },
    }
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{name}.json"
    path.write_text(json.dumps(fixture, indent=2), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--program", required=True, type=Path, help="Path to .cbl source")
    parser.add_argument("--name", required=True, help="Fixture name (used as filename)")
    parser.add_argument("--target-style", required=True, choices=("batch", "cics", "subroutine"))
    parser.add_argument("--program-id", required=True, help="PROGRAM-ID the COBOL source declares")
    parser.add_argument("--input", required=True, help="JSON object of inputs")
    parser.add_argument("--out", required=True, type=Path, help="Dir to write the fixture JSON")
    parser.add_argument("--tolerances", default="{}", help="JSON object of per-field ToleranceSpec overrides")
    args = parser.parse_args(argv)

    if shutil.which("cobc") is None:
        sys.stderr.write(
            "[record] cobc (GnuCOBOL) not on PATH — install with "
            "`brew install gnu-cobol` (macOS) or `apt-get install gnucobol` (Debian).\n"
        )
        return 1

    try:
        inputs = json.loads(args.input)
        tolerances = json.loads(args.tolerances)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[record] bad JSON: {exc}\n")
        return 1
    if not isinstance(inputs, dict):
        sys.stderr.write("[record] --input must decode to a JSON object\n")
        return 1

    with tempfile.TemporaryDirectory() as td:
        binary = _compile(args.program, Path(td))
        if binary is None:
            return 1
        exit_code, stdout, stderr = _run(binary, inputs)
        if exit_code != 0:
            sys.stderr.write(
                f"[record] COBOL run failed exit={exit_code}\nstderr:\n{stderr}\n"
            )
            return 2
        outputs = _parse_key_value_output(stdout)
        if outputs is None:
            sys.stderr.write(
                "[record] stdout wasn't key=value lines. Adapt your COBOL "
                "driver to emit one `KEY=VALUE` per line on stdout.\n"
                f"Raw stdout:\n{stdout}\n"
            )
            return 3

    path = _emit_fixture(
        name=args.name,
        target_style=args.target_style,
        program_id=args.program_id,
        inputs=inputs,
        expected=outputs,
        dest_dir=args.out,
        tolerances=tolerances,
    )
    print(f"[record] wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
