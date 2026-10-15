"""FM6 F12 — numeric LoCoMo ground truths must not be mangled.

RED-first: ``str(2024.0)`` rendered as "2024.0"; punctuation stripping then
produced the single token "20240", so a correct numeric answer scored 0.
R4-092d extends the contract: decimals inside numbers are preserved, so gold
``3.5`` no longer equals a wrong integer prediction ``35``.
"""

from __future__ import annotations

import pytest

from machina.research.locomo import _normalize_answer, token_f1


def test_float_year_ground_truth_matches_word_prediction():
    assert token_f1("2024", 2024.0) == 1.0


def test_int_ground_truth_still_matches():
    assert token_f1("2024", 2024) == 1.0
    assert token_f1("3", 3) == 1.0


def test_non_integral_float_keeps_its_decimal():
    assert token_f1("3.5", 3.5) == 1.0
    assert _normalize_answer(3.5) == "3.5"


def test_decimal_gold_does_not_merge_with_integer_prediction():
    """The decimal point is meaningful: gold 3.5 must NOT score against 35.

    RED before R4-092d: punctuation stripping collapsed "3.5" to "35", so a
    wrong integer prediction earned a perfect F1 against a decimal gold.
    """
    assert token_f1("35", 3.5) == 0.0
    assert token_f1("3.5", "35") == 0.0
    assert _normalize_answer("35") != _normalize_answer("3.5")


def test_string_ground_truth_unchanged():
    assert token_f1("2024", "2024") == 1.0
    assert _normalize_answer("Toronto, 2024!") == "toronto 2024"


@pytest.mark.parametrize(
    "value,expected",
    [
        # Only digits-adjacent dots are decimal points; every other
        # punctuation character (including standalone dots) is stripped.
        ("3.5", "3.5"),
        ("3.5.", "3.5"),
        ("$3.50!", "3.50"),
        ("1,234.5 dollars", "1234.5 dollars"),
        ("e.g. version 1.2.3", "eg version 1.2.3"),
        ("...", ""),
    ],
)
def test_normalization_strips_standalone_punctuation_only(value, expected):
    assert _normalize_answer(value) == expected


def test_bool_ground_truth_is_not_numeric_render():
    # Booleans are ints in Python but must stay textual.
    assert _normalize_answer(True) == "true"


@pytest.mark.parametrize(
    "value,expected",
    [
        (2024.0, "2024"),
        (2024.5, "2024.5"),
        (0.5, "0.5"),
        (0.0, "0"),
        (100, "100"),
        (1e21, "1e21"),
    ],
)
def test_numeric_render_uses_compact_format(value, expected):
    assert _normalize_answer(value) == expected
