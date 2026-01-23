"""Tests for CalibrationMode.DEFAULT behavior.

DEFAULT is defined as: use only the external temperature sensor to compute
an offset/setpoint correction, without any MPC/TPI/PID/heuristic adjustments.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.utils.const import CalibrationMode


@pytest.fixture
def bt_default_mode():
    """Return a minimal BetterThermostat mock configured for DEFAULT mode."""
    bt = MagicMock()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = 0.5
    bt.attr_hvac_action = None
    bt.cur_temp = 20.0

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _entity_id, offset: float(
        offset
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _entity_id, temperature: float(temperature)
    )

    bt.real_trvs = {
        "climate.trv": {
            "advanced": {"calibration_mode": CalibrationMode.DEFAULT},
            "current_temperature": 22.0,
            "last_calibration": 2.0,
            "local_calibration_step": 0.1,
            "local_calibration_min": -5.0,
            "local_calibration_max": 5.0,
            "target_temp_step": 0.5,
            "min_temp": 5.0,
            "max_temp": 30.0,
            "model_quirks": quirks,
        }
    }
    return bt


def test_default_local_calibration_computes_pure_offset(bt_default_mode):
    """DEFAULT local calibration: (external - trv) + current_offset."""

    # Important: DEFAULT should not require a target temperature
    bt_default_mode.bt_target_temp = None

    # external=20, trv=22, current_offset=2 -> new=0
    new_offset = calculate_calibration_local(bt_default_mode, "climate.trv")
    assert new_offset == pytest.approx(0.0)

    # No calibration balance/controller side-effects in DEFAULT
    assert "calibration_balance" not in bt_default_mode.real_trvs["climate.trv"]


def test_default_setpoint_calibration_skips_controller_adjustments(bt_default_mode):
    """DEFAULT setpoint calibration should be the base correction only."""

    bt_default_mode.bt_target_temp = 21.0

    # base formula is: (target - external) + trv = 23
    setpoint = calculate_calibration_setpoint(bt_default_mode, "climate.trv")
    assert setpoint == pytest.approx(23.0)

    assert "calibration_balance" not in bt_default_mode.real_trvs["climate.trv"]
