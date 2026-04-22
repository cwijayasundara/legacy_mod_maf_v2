"""COBOL-aware field comparators.

The tolerances here encode the quiet places where COBOL and Java
disagree about values that are "the same":

* **Trailing spaces** — COBOL space-pads ``PIC X(n)`` to the declared
  width; Java returns whatever the program produces. Compare trimmed
  by default.
* **Scale** — ``PIC 9(5)V99`` expects exactly 2 decimal digits, including
  trailing zeros (``3600.00`` not ``3600``). BigDecimal equality in Java
  is scale-sensitive; ``compareTo`` isn't. We mirror ``compareTo``
  semantics and verify scale via a separate rule.
* **Sign** — signed PICs may be stored as leading sign, trailing sign,
  or embedded overpunch. Normalize to ``"-"`` prefix before comparing.
* **Case** — ``PIC X(n)`` fields are often implicitly upper-case on
  mainframe; opt-in via ``case_insensitive``.

Returning ``FieldComparison`` (not just a bool) lets the diff engine
explain *why* a field didn't match — critical for human triage of
migration discrepancies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .fixtures import ToleranceSpec

_PIC_DECIMAL_RX = re.compile(r"(?:^|[^\d])9\(?(\d+)\)?(?:V9\(?(\d+)\)?)?", re.IGNORECASE)
_PIC_SIGNED_RX = re.compile(r"^\s*S", re.IGNORECASE)
_PIC_X_RX = re.compile(r"X\(?(\d+)\)?", re.IGNORECASE)


@dataclass(frozen=True)
class FieldComparison:
    """Outcome of one field comparison."""

    matched: bool
    reason: str = ""
    normalized_expected: str = ""
    normalized_actual: str = ""


def compare_field(
    expected: Any,
    actual: Any,
    spec: ToleranceSpec,
) -> FieldComparison:
    """Compare ``expected`` vs ``actual`` under ``spec``.

    A spec with ``kind="picture"`` resolves to the right concrete kind
    by inspecting ``spec.pic``. Everything else uses the kind as given.
    """
    kind = _resolve_kind(spec)
    if kind == "decimal":
        return _compare_decimal(expected, actual, spec)
    if kind == "int":
        return _compare_int(expected, actual, spec)
    if kind == "bool":
        return _compare_bool(expected, actual, spec)
    return _compare_string(expected, actual, spec)


# --------------------------------------------------------------------------- #
# Kind resolution
# --------------------------------------------------------------------------- #


def _resolve_kind(spec: ToleranceSpec) -> str:
    if spec.kind != "picture":
        return spec.kind
    pic = (spec.pic or "").strip()
    if not pic:
        return "string"
    if _PIC_DECIMAL_RX.search(pic):
        # V implies decimal; otherwise integer.
        return "decimal" if "V" in pic.upper() else "int"
    return "string"


def _decimal_scale(spec: ToleranceSpec) -> int:
    if spec.scale is not None:
        return spec.scale
    m = _PIC_DECIMAL_RX.search(spec.pic or "")
    if m and m.group(2):
        return int(m.group(2))
    return 0


def _is_signed(spec: ToleranceSpec) -> bool:
    if spec.signed:
        return True
    return bool(_PIC_SIGNED_RX.match(spec.pic or ""))


# --------------------------------------------------------------------------- #
# Comparators
# --------------------------------------------------------------------------- #


def _compare_string(expected: Any, actual: Any, spec: ToleranceSpec) -> FieldComparison:
    e = _normalize_string(expected, spec)
    a = _normalize_string(actual, spec)
    if e == a:
        return FieldComparison(True, "string equal", e, a)
    return FieldComparison(False, "string differs", e, a)


def _normalize_string(value: Any, spec: ToleranceSpec) -> str:
    s = "" if value is None else str(value)
    if spec.trim:
        s = s.rstrip(" ")
    if spec.case_insensitive:
        s = s.upper()
    return s


def _compare_decimal(expected: Any, actual: Any, spec: ToleranceSpec) -> FieldComparison:
    try:
        e = _to_decimal(expected, spec)
    except (InvalidOperation, TypeError, ValueError) as exc:
        return FieldComparison(False, f"expected not a decimal: {exc}", str(expected), str(actual))
    try:
        a = _to_decimal(actual, spec)
    except (InvalidOperation, TypeError, ValueError) as exc:
        return FieldComparison(False, f"actual not a decimal: {exc}", str(expected), str(actual))

    scale = _decimal_scale(spec)
    # Compare numeric values (scale-insensitive), then verify scale.
    abs_tol = Decimal(str(spec.abs_tol)) if spec.abs_tol else Decimal("0")
    if abs(e - a) > abs_tol:
        return FieldComparison(False, f"decimal value differs (|Δ|={abs(e - a)} > abs_tol={abs_tol})", str(e), str(a))

    # Scale check only when fixture declared a scale; otherwise don't care.
    if spec.scale is not None or (spec.pic and "V" in spec.pic.upper()):
        if _scale_of(a) > scale:
            return FieldComparison(False, f"actual has more decimal digits than PIC scale ({scale})", str(e), str(a))
    return FieldComparison(True, "decimal equal within tolerance", str(e), str(a))


def _compare_int(expected: Any, actual: Any, spec: ToleranceSpec) -> FieldComparison:
    try:
        e = _to_int(expected, spec)
        a = _to_int(actual, spec)
    except (TypeError, ValueError) as exc:
        return FieldComparison(False, f"not an integer: {exc}", str(expected), str(actual))
    if e == a:
        return FieldComparison(True, "int equal", str(e), str(a))
    return FieldComparison(False, f"int differs ({e} vs {a})", str(e), str(a))


def _compare_bool(expected: Any, actual: Any, spec: ToleranceSpec) -> FieldComparison:
    e = _to_bool(expected)
    a = _to_bool(actual)
    if e == a:
        return FieldComparison(True, "bool equal", str(e), str(a))
    return FieldComparison(False, "bool differs", str(e), str(a))


# --------------------------------------------------------------------------- #
# Value coercion
# --------------------------------------------------------------------------- #


def _strip_sign(text: str, spec: ToleranceSpec) -> str:
    """COBOL stores signs in several places (leading ``-``, trailing
    ``-``, overpunch in last digit). Only the first two are portable;
    overpunch requires caller pre-processing.
    """
    if not _is_signed(spec):
        return text
    stripped = text.strip()
    if stripped.endswith("-"):
        return "-" + stripped[:-1]
    if stripped.endswith("+"):
        return stripped[:-1]
    return stripped


def _to_decimal(value: Any, spec: ToleranceSpec) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Don't go through float's repr — it drops precision.
        return Decimal(str(value))
    s = str(value).strip()
    if not s:
        raise ValueError("empty string is not a decimal")
    s = _strip_sign(s, spec)
    return Decimal(s)


def _to_int(value: Any, spec: ToleranceSpec) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    s = str(value).strip()
    s = _strip_sign(s, spec)
    return int(s)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().upper()
    if s in {"Y", "YES", "TRUE", "T", "1"}:
        return True
    if s in {"N", "NO", "FALSE", "F", "0", ""}:
        return False
    raise ValueError(f"cannot interpret as bool: {value!r}")


def _scale_of(value: Decimal) -> int:
    _, _, exp = value.as_tuple()
    return -exp if exp < 0 else 0


__all__ = ["FieldComparison", "compare_field"]
