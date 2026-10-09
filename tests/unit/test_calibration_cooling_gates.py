"""Calibration gates that fire on an idle heating decision also fire while cooling.

A TRV valve that opens while the cooler runs works against it, so every gate
that exists because Better Thermostat is not calling for heat has to take the
same arm for ``HVACAction.COOLING`` as for ``HVACAction.IDLE``.
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


def build_bt(
    calibration_mode,
    hvac_action,
    room_temperature,
    heat_target_temperature=21.0,
    trv_temperature=21.0,
    tolerance=0.0,
    step=0.5,
    protect_overheating=False,
):
    """Return a BetterThermostat mock carrying a single configured TRV."""
    bt = ThermostatStandIn()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.attr_hvac_action = hvac_action
    bt.hvac_action = hvac_action
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
    bt.state_mgr = StateManager(MagicMock(), "cooling_gates")

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
                "min_local_calibration": -5.0,
                "max_local_calibration": 5.0,
                "target_temp_step": step,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def test_cooling_rounds_setpoint_toward_closed():
    """A cooling room rounds the setpoint down, as an idle one does.

    Room 21.05 against a TRV reading 20.9 on a 0.5 K grid: rounding to the
    nearest step writes 21.0, above the TRV's own reading, which calls for
    heat while the cooler runs.
    """
    kwargs = {
        "calibration_mode": CalibrationMode.DEFAULT,
        "room_temperature": 21.05,
        "trv_temperature": 20.9,
    }
    idle = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )
    nearest = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.OFF, **kwargs), ENTITY_ID
    )

    assert idle == pytest.approx(20.5)
    assert cooling == pytest.approx(20.5)
    assert nearest == pytest.approx(21.0)
    assert cooling < 20.9


def test_cooling_rounds_local_offset_toward_closed():
    """A cooling room rounds the calibration offset up, as an idle one does.

    The offset works inversely to the setpoint, so the closing direction is
    up; rounding to the nearest step leaves the TRV reading its own room
    temperature and calling for heat.
    """
    kwargs = {
        "calibration_mode": CalibrationMode.DEFAULT,
        "room_temperature": 21.05,
        "trv_temperature": 20.9,
    }
    idle = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )
    nearest = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.OFF, **kwargs), ENTITY_ID
    )

    assert idle == pytest.approx(0.5)
    assert cooling == pytest.approx(0.5)
    assert nearest == pytest.approx(0.0)
    assert 20.9 + cooling >= 21.0


def test_cooling_applies_tolerance_delay_to_local_offset():
    """The tolerance delay lifts the offset while cooling, as it does when idle."""
    kwargs = {
        "calibration_mode": CalibrationMode.NO_CALIBRATION,
        "room_temperature": 21.4,
        "trv_temperature": 22.0,
        "tolerance": 0.5,
        "step": 0.1,
    }
    idle = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )
    undelayed = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.OFF, **kwargs), ENTITY_ID
    )

    assert idle == pytest.approx(0.4)
    assert cooling == pytest.approx(0.4)
    assert undelayed == pytest.approx(-0.6)


def test_cooling_applies_tolerance_delay_to_setpoint():
    """The tolerance delay lowers the setpoint while cooling, as it does when idle.

    The delay reaches the setpoint only below the heating target, which the
    heating-target floor in ``compute_hvac_action`` keeps out of the cooling
    report; the arm is exercised here directly on the calibration function so
    the two calibration paths carry the same condition.
    """
    kwargs = {
        "calibration_mode": CalibrationMode.NO_CALIBRATION,
        "room_temperature": 20.5,
        "trv_temperature": 21.0,
        "tolerance": 0.5,
        "step": 0.1,
    }
    idle = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )
    undelayed = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.OFF, **kwargs), ENTITY_ID
    )

    assert idle == pytest.approx(20.5)
    assert cooling == pytest.approx(20.5)
    assert undelayed == pytest.approx(21.5)


def test_overheating_protection_applies_to_idle_only():
    """The overheating term counts from the heating target and stays idle-only.

    It is sized against the heating tolerance and measured from
    ``heating target + tolerance``; the cooling arm closes the valve through
    its own gates and does not take the term.
    """
    kwargs = {
        "calibration_mode": CalibrationMode.NO_CALIBRATION,
        "room_temperature": 23.0,
        "trv_temperature": 21.0,
        "tolerance": 0.5,
        "protect_overheating": True,
    }
    idle_setpoint = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling_setpoint = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )
    idle_offset = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling_offset = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )

    assert cooling_setpoint == pytest.approx(19.0)
    assert idle_setpoint == pytest.approx(7.0)
    assert cooling_offset == pytest.approx(2.0)
    assert idle_offset > cooling_offset
    # Both arms hold the valve shut: the TRV reads 21.0 against a 21.0 target.
    assert cooling_setpoint < 21.0
    assert 21.0 + cooling_offset >= 21.0


@pytest.mark.parametrize("calibration_mode", list(CalibrationMode))
@pytest.mark.parametrize("step", [0.1, 0.5, 1.0])
@pytest.mark.parametrize("tolerance", [0.0, 0.3, 0.5])
@pytest.mark.parametrize("room_temperature", [21.05, 21.3, 22.0, 23.7, 24.2, 26.4])
@pytest.mark.parametrize("trv_temperature", [20.0, 20.9, 21.0, 22.5])
def test_cooling_never_opens_further_than_idle(
    calibration_mode, step, tolerance, room_temperature, trv_temperature
):
    """Cooling never commands a more open valve than the same idle room does."""
    kwargs = {
        "calibration_mode": calibration_mode,
        "room_temperature": room_temperature,
        "trv_temperature": trv_temperature,
        "tolerance": tolerance,
        "step": step,
    }
    idle_setpoint = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling_setpoint = calculate_calibration_setpoint(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )
    idle_offset = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.IDLE, **kwargs), ENTITY_ID
    )
    cooling_offset = calculate_calibration_local(
        build_bt(hvac_action=HVACAction.COOLING, **kwargs), ENTITY_ID
    )

    assert cooling_setpoint <= idle_setpoint
    assert cooling_offset >= idle_offset
