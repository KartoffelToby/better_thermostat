"""Idle overheating protection may close the valve further, but not open it.

The adjustment counts from ``heating target + tolerance``. Below that line an
idle room is where Better Thermostat wants it, so the term contributes
nothing; above it the setpoint drops and the local offset rises.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.state_manager import StateManager
from tests.factories import ThermostatStandIn, make_state, trv_from_legacy_dict

ENTITY_ID = "climate.trv"
TARGET_TEMP = 19.0


def build_bt(
    *,
    room_temperature,
    trv_temperature,
    calibration_mode=CalibrationMode.AGGRESSIVE_CALIBRATION,
    protect_overheating=True,
    heat_target_temperature=TARGET_TEMP,
    tolerance=0.3,
    step=1.0,
):
    """Return an idle thermostat carrying a single configured TRV."""
    bt = ThermostatStandIn()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.attr_hvac_action = HVACAction.IDLE
    bt.hvac_action = HVACAction.IDLE
    bt.room_temperature = room_temperature
    bt.room_temperature_filtered = None
    bt.heat_target_temperature = heat_target_temperature
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.window_open = False
    bt.contact_open = False
    bt.temperature_slope = None
    bt.heating_power = 0.04
    bt.heat_loss_rate = 0.02
    bt.hass = MagicMock()
    bt.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    bt.hass.states.get.return_value = None
    bt.kernel_state = make_state()
    bt.clock = FakeClock()
    bt.state_mgr = StateManager(MagicMock(), "overheating_idle")

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = (
        lambda _self, _entity, calibration_offset: float(calibration_offset)
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _entity, temperature: float(temperature)
    )

    bt.real_trvs = {
        ENTITY_ID: trv_from_legacy_dict(
            ENTITY_ID,
            {
                "advanced": {
                    "calibration_mode": calibration_mode,
                    "protect_overheating": protect_overheating,
                },
                "current_temperature": trv_temperature,
                "last_calibration": 0.0,
                "local_calibration_step": step,
                "min_local_calibration": -6.0,
                "max_local_calibration": 6.0,
                "target_temp_step": step,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def test_idle_at_the_target_keeps_the_setpoint_at_the_trv_reading():
    """Room and TRV at the 19 °C target, tolerance 0.3, 1 °C steps.

    The unadjusted setpoint is 19 °C, which a TRV reading 19 °C keeps shut.
    """
    result = calculate_calibration_setpoint(
        build_bt(room_temperature=19.0, trv_temperature=19.0), ENTITY_ID
    )

    assert result == pytest.approx(19.0)


def test_idle_above_the_target_lowers_the_setpoint():
    """Room and TRV at 20 °C, 0.7 K over target + tolerance: 20 - 5.6 → 13 °C."""
    result = calculate_calibration_setpoint(
        build_bt(room_temperature=20.0, trv_temperature=20.0), ENTITY_ID
    )

    assert result == pytest.approx(13.0)


def test_idle_at_the_target_keeps_the_local_offset_at_zero():
    """Room and TRV at the 19 °C target: the TRV keeps reading 19 °C, not 17 °C."""
    result = calculate_calibration_local(
        build_bt(room_temperature=19.0, trv_temperature=19.0), ENTITY_ID
    )

    assert result == pytest.approx(0.0)


def test_idle_above_the_target_raises_the_local_offset():
    """Room and TRV at 20 °C: 0 + 5.6 rounds up to the 6 K offset limit."""
    result = calculate_calibration_local(
        build_bt(room_temperature=20.0, trv_temperature=20.0), ENTITY_ID
    )

    assert result == pytest.approx(6.0)


@pytest.mark.parametrize("calibration_mode", list(CalibrationMode))
@pytest.mark.parametrize("step", [0.1, 0.5, 1.0])
@pytest.mark.parametrize("tolerance", [0.0, 0.3, 0.5])
@pytest.mark.parametrize("room_temperature", [18.4, 18.9, 19.0, 19.2, 19.5, 20.0, 21.3])
@pytest.mark.parametrize("trv_temperature", [18.0, 19.0, 20.5])
def test_protection_does_not_open_the_valve_further(
    calibration_mode, step, tolerance, room_temperature, trv_temperature
):
    """The protection leaves both values alone up to the line and closes above it."""
    kwargs = {
        "calibration_mode": calibration_mode,
        "room_temperature": room_temperature,
        "trv_temperature": trv_temperature,
        "tolerance": tolerance,
        "step": step,
    }
    protected_setpoint = calculate_calibration_setpoint(
        build_bt(protect_overheating=True, **kwargs), ENTITY_ID
    )
    plain_setpoint = calculate_calibration_setpoint(
        build_bt(protect_overheating=False, **kwargs), ENTITY_ID
    )
    protected_offset = calculate_calibration_local(
        build_bt(protect_overheating=True, **kwargs), ENTITY_ID
    )
    plain_offset = calculate_calibration_local(
        build_bt(protect_overheating=False, **kwargs), ENTITY_ID
    )

    assert protected_setpoint is not None
    assert plain_setpoint is not None
    assert protected_offset is not None
    assert plain_offset is not None
    if room_temperature <= TARGET_TEMP + tolerance:
        assert protected_setpoint == pytest.approx(plain_setpoint)
        assert protected_offset == pytest.approx(plain_offset)
    else:
        assert protected_setpoint <= plain_setpoint
        assert protected_offset >= plain_offset
