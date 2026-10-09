"""Characterization tests for the HEATING_POWER post-adjustment block.

Both calibration channels (local offset and setpoint) carry the same
heating-power machinery: publish a valve intent when direct valve
control is available and hold the channel value so the calibration does
not counteract it, otherwise fall back to the channel's legacy
valve-position math. These tests pin that behavior for both channels.
"""

from unittest.mock import MagicMock, patch

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.calibration import (
    _heating_power_adjustment,
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.helpers import (
    heating_power_valve_position,
)
from tests.factories import ThermostatStandIn, make_state, trv_from_legacy_dict

ENTITY_ID = "climate.test_trv"
_CAL = "custom_components.better_thermostat.calibration"

VALVE_INTENT_SOURCE = {"source": "heating_power_calibration"}


def _make_bt(
    hvac_action,
    *,
    room_temperature=20.0,
    heat_target_temperature=21.0,
    tolerance=0.3,
    trv_temperature=21.0,
    last_calibration=0.0,
):
    """Mock entity in HEATING_POWER mode, mirroring the calibration fixtures."""
    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.hvac_action = hvac_action
    bt.room_temperature = room_temperature
    bt.heat_target_temperature = heat_target_temperature
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.bt_hvac_mode = HVACMode.OFF

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _eid, calibration_offset: (
        float(calibration_offset)
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _eid, temperature: float(temperature)
    )

    bt.real_trvs = {
        ENTITY_ID: trv_from_legacy_dict(
            ENTITY_ID,
            {
                "advanced": {
                    "calibration_mode": CalibrationMode.HEATING_POWER_CALIBRATION,
                    "protect_overheating": False,
                },
                "current_temperature": trv_temperature,
                "last_calibration": last_calibration,
                "local_calibration_step": 0.1,
                "min_local_calibration": -5.0,
                "max_local_calibration": 5.0,
                "target_temp_step": 0.1,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def _run(channel, bt):
    if channel == "local":
        return calculate_calibration_local(bt, ENTITY_ID)
    return calculate_calibration_setpoint(bt, ENTITY_ID)


class TestWithDirectValveControl:
    """With valve support the channel value is held and an intent published."""

    @pytest.mark.parametrize(
        ("channel", "held_value"), [("local", 0.0), ("setpoint", 21.0)]
    )
    def test_idle_publishes_closed_valve_and_holds_the_value(self, channel, held_value):
        """Not heating: valve intent 0 %, channel value held, no post tweaks."""
        bt = _make_bt(HVACAction.IDLE)
        with patch(f"{_CAL}._supports_direct_valve_control", return_value=True):
            result = _run(channel, bt)
        assert result == pytest.approx(held_value)
        assert bt.real_trvs[ENTITY_ID].calibration_balance == {
            "valve_percent": 0,
            "apply_valve": True,
            "debug": VALVE_INTENT_SOURCE,
        }

    @pytest.mark.parametrize(
        ("channel", "held_value"), [("local", 0.0), ("setpoint", 21.0)]
    )
    def test_heating_publishes_the_valve_position_and_holds_the_value(
        self, channel, held_value
    ):
        """Heating: valve position becomes the intent, channel value held."""
        bt = _make_bt(HVACAction.HEATING)
        with (
            patch(f"{_CAL}._supports_direct_valve_control", return_value=True),
            patch(f"{_CAL}.heating_power_valve_position", return_value=0.42),
        ):
            result = _run(channel, bt)
        assert result == pytest.approx(held_value)
        assert bt.real_trvs[ENTITY_ID].calibration_balance == {
            "valve_percent": 42,
            "apply_valve": True,
            "debug": VALVE_INTENT_SOURCE,
        }

    def test_both_channels_publish_the_identical_intent(self):
        """The published valve-intent payload is channel-independent."""
        intents = []
        for channel in ("local", "setpoint"):
            bt = _make_bt(HVACAction.HEATING)
            with (
                patch(f"{_CAL}._supports_direct_valve_control", return_value=True),
                patch(f"{_CAL}.heating_power_valve_position", return_value=0.7),
            ):
                _run(channel, bt)
            intents.append(bt.real_trvs[ENTITY_ID].calibration_balance)
        assert intents[0] == intents[1]


class TestWithoutDirectValveControl:
    """Without valve support the legacy per-channel math applies."""

    def test_local_heating_uses_the_legacy_offset_math(self):
        """Compute last_cal - ((cal_min + trv_temperature) * valve_position)."""
        bt = _make_bt(HVACAction.HEATING)
        with (
            patch(f"{_CAL}._supports_direct_valve_control", return_value=False),
            patch(f"{_CAL}.heating_power_valve_position", return_value=0.5),
        ):
            result = calculate_calibration_local(bt, ENTITY_ID)
        # 0.0 - ((-5.0 + 21.0) * 0.5) = -8.0; range is the safety hull's job.
        assert result == pytest.approx(-8.0)
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None

    def test_setpoint_heating_uses_the_legacy_setpoint_math(self):
        """Compute trv_temperature + ((max_temp - trv_temperature) * valve_position)."""
        bt = _make_bt(HVACAction.HEATING)
        with (
            patch(f"{_CAL}._supports_direct_valve_control", return_value=False),
            patch(f"{_CAL}.heating_power_valve_position", return_value=0.5),
        ):
            result = calculate_calibration_setpoint(bt, ENTITY_ID)
        # 21.0 + ((30.0 - 21.0) * 0.5) = 25.5
        assert result == pytest.approx(25.5)
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None

    def test_local_idle_keeps_the_base_value_with_post_adjustments(self):
        """Not heating, no valve: base math plus the tolerance delay."""
        bt = _make_bt(HVACAction.IDLE)
        with patch(f"{_CAL}._supports_direct_valve_control", return_value=False):
            result = calculate_calibration_local(bt, ENTITY_ID)
        # base (20.0 - 21.0) + 0.0 = -1.0; idle delay adds 2 * 0.3.
        assert result == pytest.approx(-0.4)
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None

    def test_setpoint_idle_keeps_the_base_value_with_post_adjustments(self):
        """Not heating, no valve: base math plus the tolerance delay."""
        bt = _make_bt(HVACAction.IDLE)
        with patch(f"{_CAL}._supports_direct_valve_control", return_value=False):
            result = calculate_calibration_setpoint(bt, ENTITY_ID)
        # base (21.0 - 20.0) + 21.0 = 22.0; idle delay subtracts 2 * 0.3.
        assert result == pytest.approx(21.4)
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None


def _make_sensor_fallback_bt(hvac_action, *, trv_temperature):
    """HEATING_POWER entity whose room sensor is dead under SENSOR_FALLBACK.

    The effective room temperature is the reachable TRV's internal reading
    while ``room_temperature`` itself stays ``None``.
    """
    bt = _make_bt(hvac_action, room_temperature=None, trv_temperature=trv_temperature)
    bt.heating_power = 0.02
    bt.kernel_state = make_state(
        control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)
    )
    bt.hass.states.get.side_effect = lambda entity_id: (
        State(entity_id, "heat", {"current_temperature": trv_temperature})
        if entity_id == ENTITY_ID
        else None
    )
    bt.hass.config.units.temperature_unit = "°C"
    return bt


class TestUnderSensorFallback:
    """Heating power sizes the valve from the effective room temperature."""

    @pytest.mark.parametrize(
        ("channel", "held_value"), [("local", 0.0), ("setpoint", 21.0)]
    )
    def test_heating_sizes_the_valve_from_the_trv_reading(self, channel, held_value):
        """0.5 K below target at heating power 0.02 opens the valve to 40 %."""
        bt = _make_sensor_fallback_bt(HVACAction.HEATING, trv_temperature=20.5)
        with patch(f"{_CAL}._supports_direct_valve_control", return_value=True):
            result = _run(channel, bt)
        assert result == pytest.approx(held_value)
        assert bt.real_trvs[ENTITY_ID].calibration_balance == {
            "valve_percent": 40,
            "apply_valve": True,
            "debug": VALVE_INTENT_SOURCE,
        }

    def test_setpoint_without_valve_control_uses_the_trv_reading(self):
        """The legacy setpoint math runs on the valve sized from the TRV reading."""
        bt = _make_sensor_fallback_bt(HVACAction.HEATING, trv_temperature=20.5)
        with patch(f"{_CAL}._supports_direct_valve_control", return_value=False):
            result = calculate_calibration_setpoint(bt, ENTITY_ID)
        expected_fraction = heating_power_valve_position(bt, ENTITY_ID, 20.5)
        assert expected_fraction == pytest.approx(0.3992, abs=1e-4)
        # 20.5 + (30.0 - 20.5) * 0.3992 = 24.29, rounded to the 0.1 step.
        assert result == pytest.approx(24.3)
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None

    @pytest.mark.parametrize("direct_valve", [True, False])
    def test_no_room_reading_keeps_the_base_value(self, direct_valve):
        """Heating without any room reading publishes no intent and keeps the value."""
        bt = _make_bt(HVACAction.HEATING, room_temperature=None)
        bt.real_trvs[ENTITY_ID].calibration_balance = {"stale": True}
        with patch(f"{_CAL}._supports_direct_valve_control", return_value=direct_valve):
            result = _heating_power_adjustment(
                bt,
                ENTITY_ID,
                1.5,
                hold_value=21.0,
                legacy_fallback=lambda _position: pytest.fail("valve was sized"),
            )
        assert result == (1.5, False)
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None

    @pytest.mark.parametrize("missing", ["room_temperature", "heat_target_temperature"])
    def test_setpoint_without_demand_drops_the_valve_intent(self, missing):
        """No target or no room reading leaves no valve intent to replay."""
        bt = _make_bt(HVACAction.HEATING, room_temperature=None)
        if missing == "heat_target_temperature":
            bt = _make_bt(HVACAction.HEATING)
            bt.heat_target_temperature = None
        bt.real_trvs[ENTITY_ID].calibration_balance = {
            "valve_percent": 40,
            "apply_valve": True,
        }
        assert calculate_calibration_setpoint(bt, ENTITY_ID) is None
        assert bt.real_trvs[ENTITY_ID].calibration_balance is None
