"""Coerce values read back from a JSON store into the types the state holds.

A loaded store is untyped JSON: every value is ``None``, a bool, an int, a
float, a string, a list or an object. The helpers here take such a value as
``object`` and narrow it to the JSON scalar types before converting it, so
each conversion keeps the semantics of ``float()`` and ``int()`` on that
scalar: a numeric string parses, ``"nan"`` and ``"inf"`` parse as the
non-finite floats they name, and ``None``, a list or an object raise
``TypeError``.

The module imports only the standard library, so the calibration models
can use it as well as the state manager.
"""

from __future__ import annotations

from collections.abc import Mapping
import math
from typing import TypeIs

# Home Assistant's JSON encoder writes an integer only inside the 64-bit
# range orjson supports and raises TypeError on anything wider. The Store's
# write path turns that TypeError into a SerializationError, which the Store
# catches and only logs, so a single unstorable integer anywhere in the
# state leaves the config entry's file unwritten without failing the save.
MIN_STORED_INT = -(2**63)
MAX_STORED_INT = 2**64 - 1


def _not_a_scalar(value: object, target: str) -> TypeError:
    """Return the error a conversion to *target* raises for *value*."""
    return TypeError(
        f"{target}() argument must be a string or a real number, "
        f"not '{type(value).__name__}'"
    )


def is_json_object(value: object) -> TypeIs[Mapping[str, object]]:
    """Return whether *value* is a JSON object.

    A JSON object always has string keys, so a mapping read from a store
    narrows to ``Mapping[str, object]`` without inspecting its keys.

    Parameters
    ----------
    value : object
        the value to test, as read from a store

    Returns
    -------
    bool
        True when *value* is a mapping
    """
    return isinstance(value, Mapping)


def stored_float(value: object) -> float:
    """Return *value* as a float, with ``float()`` semantics on JSON scalars.

    Parameters
    ----------
    value : object
        the value to convert, as read from a store

    Returns
    -------
    float
        the converted value, which may be NaN or infinite

    Raises
    ------
    TypeError
        when *value* is not a bool, number or string
    ValueError
        when *value* is a string ``float()`` cannot parse
    OverflowError
        when *value* is an integer too large for a float
    """
    if isinstance(value, int | float | str):
        return float(value)
    raise _not_a_scalar(value, "float")


def stored_int(value: object) -> int:
    """Return *value* as an integer the store can write back.

    A JSON number wider than 64 bits is parsed as a float, and ``int()``
    turns it into an arbitrary-precision integer that the encoder refuses.
    Those raise ``ValueError`` here, so a caller handles them like any
    other field ``int()`` cannot make sense of.

    Parameters
    ----------
    value : object
        the value to convert, as read from a store

    Returns
    -------
    int
        the converted value, inside the storable range

    Raises
    ------
    TypeError
        when *value* is not a bool, number or string
    ValueError
        when ``int()`` cannot parse *value*, or the result is not storable
    OverflowError
        when *value* is an infinite float
    """
    if not isinstance(value, int | float | str):
        raise _not_a_scalar(value, "int")
    number = int(value)
    if not MIN_STORED_INT <= number <= MAX_STORED_INT:
        raise ValueError("integer outside the storable range")
    return number


def stored_count(value: object) -> int:
    """Return *value* as a storable, non-negative tally.

    A tally cannot be negative, so a negative value is as unusable as one
    the store could not write back or one that is not an integer at all;
    all three restore as 0.

    Parameters
    ----------
    value : object
        the value to convert, as read from a store

    Returns
    -------
    int
        the tally, or 0 when *value* is not a usable one
    """
    try:
        count = stored_int(value)
    except TypeError, ValueError, OverflowError:
        return 0
    return max(count, 0)


def finite_or_none(value: object) -> float | None:
    """Return *value* as a finite float, or ``None`` when it is not one.

    A missing value, one ``float()`` refuses, and NaN or infinity all
    collapse to ``None``. Non-finite numbers carry no usable learning, and
    keeping one would only feed the same unusable value back into the next
    calculation that reads it.

    Parameters
    ----------
    value : object
        the value to read, from a store or a caller

    Returns
    -------
    float | None
        the value as a finite float, or None when it is not one
    """
    if value is None:
        return None
    try:
        number = stored_float(value)
    except TypeError, ValueError, OverflowError:
        return None
    return number if math.isfinite(number) else None
