"""One reading of a TRV's calibration settings, shared by every reader.

The calibration mode and the calibration type are read in the climate
entity, the calibration, the control cycle, the outbound conversion and the
number, switch and sensor platforms. Each one reads them through
``configured_calibration_mode`` and ``configured_calibration_output``, so a
stored value means the same thing wherever it is read.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.components.climate import HVACMode
import pytest

from custom_components.better_thermostat.events.trv import convert_outbound_states
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
    CalibrationOutput,
)
from custom_components.better_thermostat.utils.entry_schema import TrvAdvanced
from custom_components.better_thermostat.utils.helpers import (
    configured_calibration_mode,
    configured_calibration_output,
)
from tests.factories import ThermostatStandIn

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


def _outbound_bt(advanced: TrvAdvanced) -> ThermostatStandIn:
    """A thermostat whose one TRV carries ``advanced``."""
    bt = ThermostatStandIn()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.heat_target_temperature = 21.0
    bt.room_temperature = 20.0
    bt.window_open = False
    bt.real_trvs = {
        "climate.trv": Trv(
            entity_id="climate.trv",
            hvac_modes=[HVACMode.HEAT, HVACMode.OFF],
            min_temp=5.0,
            max_temp=30.0,
            current_temperature=20.0,
            advanced=advanced,
        )
    }
    return bt


@pytest.mark.parametrize(
    "stored_mode", ["no_calibration", "No_Calibration", " NO_CALIBRATION"]
)
def test_no_calibration_sends_the_room_target_however_it_is_spelled(stored_mode):
    """Every spelling the calibration reads as no calibration sends the plain target.

    The calibration and the control cycle match the mode regardless of case,
    so the setpoint written to the TRV has to follow the same reading; a
    calibrated setpoint next to a calibration that runs no correction would
    write a value nothing computed for it.
    """
    bt = _outbound_bt(
        {
            CONF_CALIBRATION: CalibrationOutput.TARGET_TEMP_BASED.value,
            CONF_CALIBRATION_MODE: stored_mode,
        }
    )

    with patch(
        "custom_components.better_thermostat.events.trv.calculate_calibration_setpoint",
        return_value=25.0,
    ):
        payload = convert_outbound_states(bt, "climate.trv", HVACMode.HEAT)

    assert payload is not None
    assert payload["temperature"] == 21.0


@pytest.mark.parametrize("stored_type", [_MISSING, None, "a type from another version"])
def test_no_known_calibration_type_sends_the_room_target(stored_type):
    """A setting that selects no calibration type writes the plain room target."""
    advanced: TrvAdvanced = {
        CONF_CALIBRATION_MODE: CalibrationMode.PID_CALIBRATION.value
    }
    if stored_type is not _MISSING:
        advanced[CONF_CALIBRATION] = stored_type
    bt = _outbound_bt(advanced)

    with patch(
        "custom_components.better_thermostat.events.trv.calculate_calibration_setpoint",
        return_value=25.0,
    ):
        payload = convert_outbound_states(bt, "climate.trv", HVACMode.HEAT)

    assert payload is not None
    assert payload["temperature"] == 21.0
    assert payload.get("local_temperature_calibration") is None
