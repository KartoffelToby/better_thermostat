"""Characterization tests for the HEATING_POWER post-adjustment block.

Both calibration channels (local offset and setpoint) carry the same
heating-power machinery: publish a valve intent when direct valve
control is available and hold the channel value so the calibration does
not counteract it, otherwise fall back to the channel's legacy
valve-position math. These tests pin that behavior for both channels.
"""

from unittest.mock import MagicMock, patch

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationMode

ENTITY_ID = "climate.test_trv"
_CAL = "custom_components.better_thermostat.calibration"

VALVE_INTENT_SOURCE = {"source": "heating_power_calibration"}


def _make_bt(
    hvac_action,
    *,
    cur_temp=20.0,
    bt_target_temp=21.0,
    tolerance=0.3,
    trv_temp=21.0,
    last_calibration=0.0,
):
    """Mock entity in HEATING_POWER mode, mirroring the calibration fixtures."""
    bt = MagicMock()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.hvac_action = hvac_action
    bt.cur_temp = cur_temp
    bt.bt_target_temp = bt_target_temp
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.bt_hvac_mode = HVACMode.OFF

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _eid, offset: float(offset)
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _eid, temperature: float(temperature)
    )

    bt.real_trvs = {
        ENTITY_ID: Trv.from_legacy_dict(
            ENTITY_ID,
            {
                "advanced": {
                    "calibration_mode": CalibrationMode.HEATING_POWER_CALIBRATION,
                    "protect_overheating": False,
                },
                "current_temperature": trv_temp,
                "last_calibration": last_calibration,
                "local_calibration_step": 0.1,
                "local_calibration_min": -5.0,
                "local_calibration_max": 5.0,
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
        """Compute last_cal - ((cal_min + trv_temp) * valve_position)."""
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
        """Compute trv_temp + ((max_temp - trv_temp) * valve_position)."""
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
