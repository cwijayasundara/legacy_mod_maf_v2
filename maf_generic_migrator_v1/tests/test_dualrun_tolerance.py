"""Tests for COBOL-aware field tolerance comparators."""
from __future__ import annotations

from maf_generic_migrator_v1.platform_core.dualrun import (
    ToleranceSpec,
    compare_field,
)


# ---------------------------------------------------------------------- #
# PIC X(n) — strings with optional trim
# ---------------------------------------------------------------------- #


def test_string_trims_trailing_spaces_by_default():
    """COBOL space-pads ``PIC X(n)``; Java may not. Trim by default."""
    spec = ToleranceSpec(kind="picture", pic="X(6)")
    assert compare_field("E00042", "E00042    ", spec).matched
    assert compare_field("E00042   ", "E00042", spec).matched


def test_string_difference_is_detected():
    spec = ToleranceSpec(kind="picture", pic="X(6)")
    result = compare_field("E00042", "E00099", spec)
    assert result.matched is False
    assert "differs" in result.reason.lower()


def test_string_trim_off_catches_padding_difference():
    spec = ToleranceSpec(kind="string", trim=False)
    assert not compare_field("E00042", "E00042   ", spec).matched


def test_string_case_insensitive_opt_in():
    spec = ToleranceSpec(kind="string", case_insensitive=True)
    assert compare_field("abc", "ABC", spec).matched


# ---------------------------------------------------------------------- #
# PIC 9(n)V9(m) — decimals
# ---------------------------------------------------------------------- #


def test_decimal_equal_strict():
    spec = ToleranceSpec(kind="picture", pic="9(5)V99")
    assert compare_field("3600.00", "3600.00", spec).matched


def test_decimal_equivalent_representations_match():
    """``3600`` and ``3600.00`` are the same number; BigDecimal.compareTo
    treats them equal. The tolerance mirrors that rather than ``equals``.
    """
    spec = ToleranceSpec(kind="picture", pic="9(5)V99")
    assert compare_field("3600.00", "3600", spec).matched


def test_decimal_over_precise_actual_is_a_mismatch():
    """PIC 9(5)V99 has exactly 2 decimal digits. More digits in actual
    → mismatch, even if the value is equal.
    """
    spec = ToleranceSpec(kind="picture", pic="9(5)V99")
    result = compare_field("3600.00", "3600.001", spec)
    assert not result.matched


def test_decimal_abs_tol_allows_small_drift():
    spec = ToleranceSpec(kind="picture", pic="9(5)V99", abs_tol=0.01)
    assert compare_field("3600.00", "3600.005", spec).matched


def test_decimal_nonnumeric_value_reports_cleanly():
    spec = ToleranceSpec(kind="picture", pic="9(5)V99")
    result = compare_field("3600.00", "not a number", spec)
    assert not result.matched
    assert "decimal" in result.reason.lower()


# ---------------------------------------------------------------------- #
# PIC S9(n) — signed ints
# ---------------------------------------------------------------------- #


def test_signed_int_trailing_sign_interpreted():
    """Some COBOL dialects emit the sign as a trailing ``-``. Recognize it."""
    spec = ToleranceSpec(kind="int", signed=True)
    assert compare_field("-42", "42-", spec).matched


def test_unsigned_int_match():
    spec = ToleranceSpec(kind="int")
    assert compare_field("42", "42", spec).matched
    assert not compare_field("42", "43", spec).matched


# ---------------------------------------------------------------------- #
# bool / default
# ---------------------------------------------------------------------- #


def test_bool_y_n_normalization():
    spec = ToleranceSpec(kind="bool")
    assert compare_field("Y", "YES", spec).matched
    assert compare_field("N", "false", spec).matched
    assert not compare_field("Y", "N", spec).matched


def test_picture_without_pic_falls_back_to_string():
    """A bare ``kind=picture`` without a PIC value should compare as string."""
    spec = ToleranceSpec(kind="picture")
    assert compare_field("hello", "hello", spec).matched
    assert not compare_field("hello", "world", spec).matched


def test_result_carries_normalized_values():
    """For diff diagnostics, the FieldComparison must report the
    normalized-for-comparison values so humans see why things compare.
    """
    spec = ToleranceSpec(kind="picture", pic="X(6)")
    result = compare_field("abc   ", "abc", spec)
    assert result.normalized_expected == "abc"
    assert result.normalized_actual == "abc"
