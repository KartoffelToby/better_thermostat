"""One reading of a TRV's calibration settings per key."""

from __future__ import annotations

import pytest

from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_mode,
    configured_calibration_output,
)

_MISSING = object()


def _advanced(key: str, stored: object) -> dict[str, object]:
    """Advanced settings that store ``stored`` under ``key``, or omit it."""
    return {} if stored is _MISSING else {key: stored}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (_MISSING, DEFAULT_CALIBRATION_MODE),
        (None, DEFAULT_CALIBRATION_MODE),
        (0, CalibrationMode.DEFAULT),
        (3, DEFAULT_CALIBRATION_MODE),
        (float("nan"), DEFAULT_CALIBRATION_MODE),
        ([], DEFAULT_CALIBRATION_MODE),
        ("pid_calibration", CalibrationMode.PID_CALIBRATION),
        (CalibrationMode.MPC_CALIBRATION, CalibrationMode.MPC_CALIBRATION),
        ("  PID_Calibration ", CalibrationMode.PID_CALIBRATION),
        ("a mode from another version", None),
        ("", None),
    ],
    ids=[
        "missing",
        "null",
        "legacy-zero",
        "unmappable-number",
        "nan",
        "non-string",
        "exact-name",
        "enum-member",
        "mis-cased-name",
        "unknown-name",
        "empty-name",
    ],
)
def test_the_calibration_mode_reading(stored, expected):
    """What each stored calibration mode selects."""
    advanced = _advanced(CONF_CALIBRATION_MODE, stored)

    assert configured_calibration_mode(advanced) == expected


def test_no_advanced_settings_select_the_default_mode():
    """A TRV record without advanced settings runs the default mode."""
    assert configured_calibration_mode(None) == DEFAULT_CALIBRATION_MODE


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (_MISSING, None),
        (None, None),
        (0, None),
        ("target_temp_based", CalibrationOutput.TARGET_TEMP_BASED),
        ("local_calibration_based", CalibrationOutput.LOCAL_BASED),
        (CalibrationOutput.DIRECT_VALVE_BASED, CalibrationOutput.DIRECT_VALVE_BASED),
        ("Direct_Valve_Based", None),
        ("a type from another version", None),
    ],
    ids=[
        "missing",
        "null",
        "number",
        "target-temp",
        "offset",
        "enum-member",
        "mis-cased-name",
        "unknown-name",
    ],
)
def test_the_calibration_type_reading(stored, expected):
    """What each stored calibration type selects."""
    advanced = _advanced(CONF_CALIBRATION, stored)

    assert configured_calibration_output(advanced) == expected


def test_no_advanced_settings_select_no_calibration_type():
    """A TRV record without advanced settings selects no calibration type."""
    assert configured_calibration_output(None) is None
