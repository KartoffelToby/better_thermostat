"""Idle overheating protection may lower the setpoint, never raise it."""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationMode

ENTITY_ID = "climate.trv"


def build_bt(*, cur_temp, trv_temp, bt_target_temp=19.0, tolerance=0.3, step=1.0):
    """Return an idle Aggressive thermostat with overheating protection on."""
    bt = MagicMock()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.attr_hvac_action = HVACAction.IDLE
    bt.hvac_action = HVACAction.IDLE
    bt.cur_temp = cur_temp
    bt.cur_temp_filtered = None
    bt.bt_target_temp = bt_target_temp
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.outdoor_sensor = None
    bt.weather_entity = None

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _entity, offset: float(
        offset
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _entity, temperature: float(temperature)
    )

    bt.real_trvs = {
        ENTITY_ID: Trv.from_legacy_dict(
            ENTITY_ID,
            {
                "advanced": {
                    "calibration_mode": CalibrationMode.AGGRESIVE_CALIBRATION,
                    "protect_overheating": True,
                },
                "current_temperature": trv_temp,
                "last_calibration": 0.0,
                "local_calibration_step": step,
                "local_calibration_min": -6.0,
                "local_calibration_max": 6.0,
                "target_temp_step": step,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def test_idle_at_the_target_does_not_raise_the_setpoint():
    """An idle room at the target is not given a setpoint above the TRV.

    Target 19 °C, tolerance 0.3, external and TRV both 19 °C, 1 °C steps.
    The unadjusted setpoint is 19 °C.
    """
    result = calculate_calibration_setpoint(
        build_bt(cur_temp=19.0, trv_temp=19.0), ENTITY_ID
    )

    assert result == pytest.approx(19.0)
    assert result <= 19.0


def test_idle_above_the_target_still_lowers_the_setpoint():
    """An idle room above the target plus tolerance still gets a lower setpoint.

    External and TRV both 20 °C, target 19 °C, tolerance 0.3, 1 °C steps.
    """
    result = calculate_calibration_setpoint(
        build_bt(cur_temp=20.0, trv_temp=20.0), ENTITY_ID
    )

    assert result == pytest.approx(13.0)
    assert result < 20.0
