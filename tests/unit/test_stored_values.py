"""Tests for the coercers that read values back from a JSON store."""

from __future__ import annotations

import json
import math

import pytest

from custom_components.better_thermostat.utils.stored_values import (
    MAX_STORED_INT,
    MIN_STORED_INT,
    finite_or_none,
    is_json_object,
    stored_count,
    stored_float,
    stored_int,
)

NAN = float("nan")
INF = float("inf")

# Each row: input, stored_float, stored_int, stored_count, finite_or_none.
# An exception class means the call raises it; NAN stands for a NaN result.
CASES: list[tuple[object, object, object, object, object]] = [
    (None, TypeError, TypeError, 0, None),
    (True, 1.0, 1, 1, 1.0),
    (False, 0.0, 0, 0, 0.0),
    (0, 0.0, 0, 0, 0.0),
    (7, 7.0, 7, 7, 7.0),
    (-1, -1.0, -1, 0, -1.0),
    (MAX_STORED_INT, float(MAX_STORED_INT), MAX_STORED_INT, MAX_STORED_INT, 2.0**64),
    (MAX_STORED_INT + 1, 2.0**64, ValueError, 0, 2.0**64),
    (MIN_STORED_INT, -(2.0**63), MIN_STORED_INT, 0, -(2.0**63)),
    (MIN_STORED_INT - 1, -(2.0**63), ValueError, 0, -(2.0**63)),
    (10**400, OverflowError, ValueError, 0, None),
    (1.5, 1.5, 1, 1, 1.5),
    (-1.5, -1.5, -1, 0, -1.5),
    (2.9, 2.9, 2, 2, 2.9),
    (1e30, 1e30, ValueError, 0, 1e30),
    (NAN, NAN, ValueError, 0, None),
    (INF, INF, OverflowError, 0, None),
    (-INF, -INF, OverflowError, 0, None),
    ("1", 1.0, 1, 1, 1.0),
    (" 2 ", 2.0, 2, 2, 2.0),
    ("-3", -3.0, -3, 0, -3.0),
    ("1_000", 1000.0, 1000, 1000, 1000.0),
    ("1.5", 1.5, ValueError, 0, 1.5),
    ("1e3", 1000.0, ValueError, 0, 1000.0),
    ("1e999", INF, ValueError, 0, None),
    ("nan", NAN, ValueError, 0, None),
    ("NaN", NAN, ValueError, 0, None),
    ("inf", INF, ValueError, 0, None),
    ("-Infinity", -INF, ValueError, 0, None),
    ("", ValueError, ValueError, 0, None),
    ("later", ValueError, ValueError, 0, None),
    ("0x10", ValueError, ValueError, 0, None),
    ([], TypeError, TypeError, 0, None),
    ([1.5], TypeError, TypeError, 0, None),
    ({}, TypeError, TypeError, 0, None),
    ({"x": 1}, TypeError, TypeError, 0, None),
    (json.loads("1e400"), INF, OverflowError, 0, None),
]


def _check(fn, value: object, expected: object) -> None:
    """Assert that ``fn(value)`` returns or raises what *expected* names."""
    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            fn(value)
        return
    result = fn(value)
    if isinstance(expected, float) and math.isnan(expected):
        assert isinstance(result, float)
        assert math.isnan(result)
        return
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    ("value", "as_float", "as_int", "as_count", "as_finite"), CASES, ids=repr
)
def test_coercers_follow_float_and_int_on_every_json_value(
    value: object, as_float: object, as_int: object, as_count: object, as_finite: object
) -> None:
    """Each coercer keeps the semantics of ``float()`` or ``int()`` on JSON."""
    _check(stored_float, value, as_float)
    _check(stored_int, value, as_int)
    _check(stored_count, value, as_count)
    _check(finite_or_none, value, as_finite)


def test_a_non_scalar_names_its_type_in_the_error() -> None:
    """The TypeError for a list says what was passed, like ``float()``'s does."""
    with pytest.raises(TypeError, match="not 'list'"):
        stored_float([1.0])
    with pytest.raises(TypeError, match="^int\\(\\) .* not 'dict'"):
        stored_int({})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({}, True),
        ({"a": 1}, True),
        (json.loads('{"a": [1, 2]}'), True),
        ([], False),
        (None, False),
        ("{}", False),
        (1, False),
    ],
    ids=repr,
)
def test_is_json_object_accepts_exactly_the_mappings(
    value: object, expected: bool
) -> None:
    """Only a mapping reads as a JSON object."""
    assert is_json_object(value) is expected
