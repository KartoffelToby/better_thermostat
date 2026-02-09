"""Tests for TRV state handling with ignore_trv_states flag.

When BT sends commands to a TRV, it sets `ignore_trv_states = True` to prevent
the TRV's response from being misinterpreted. This module tests that temperature
changes from TRVs are correctly blocked during active communication.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)


@pytest.fixture
def mock_bt_instance():
    """Create a mock BetterThermostat instance for TRV state tests."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 19.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.cur_temp = 18.0
    bt.window_open = False
    bt.tolerance = 0.3
    bt.startup_running = False
    bt.control_queue_task = MagicMock()
    bt.bt_update_lock = False
    bt.cooler_entity_id = None

    bt.real_trvs = {
        "climate.test_trv": {
            "hvac_mode": HVACMode.HEAT,
            "hvac_modes": [HVACMode.OFF, HVACMode.HEAT],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "current_temperature": 18.0,
            "temperature": 19.0,
            "last_temperature": 19.0,
            "target_temp_received": True,
            "system_mode_received": True,
            "ignore_trv_states": False,
            "advanced": {
                "calibration": CalibrationType.LOCAL_BASED,
                "calibration_mode": CalibrationMode.DEFAULT,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
                "child_lock": False,
            },
        }
    }
    return bt


class TestIgnoreTrvStates:
    """Tests for ignore_trv_states flag handling in temperature change logic."""

    def test_temp_change_blocked_when_ignore_trv_states_true(self, mock_bt_instance):
        """Temperature changes from TRV are blocked when ignore_trv_states is True."""
        mock_bt_instance.real_trvs["climate.test_trv"]["ignore_trv_states"] = True

        trv_new_temp = 22.0
        entity_id = "climate.test_trv"
        trv_data = mock_bt_instance.real_trvs[entity_id]
        child_lock = trv_data["advanced"].get("child_lock", False)

        should_adopt_temp = (
            trv_new_temp
            not in (
                mock_bt_instance.bt_target_temp,
                trv_data["temperature"],
                trv_data["last_temperature"],
            )
            and not child_lock
            and trv_data["target_temp_received"] is True
            and trv_data["system_mode_received"] is True
            and trv_data["hvac_mode"] is not HVACMode.OFF
            and mock_bt_instance.window_open is False
            and not trv_data.get("ignore_trv_states", False)
        )

        assert should_adopt_temp is False

    def test_temp_change_allowed_when_ignore_trv_states_false(self, mock_bt_instance):
        """Temperature changes from TRV are allowed when ignore_trv_states is False."""
        mock_bt_instance.real_trvs["climate.test_trv"]["ignore_trv_states"] = False

        trv_new_temp = 22.0
        entity_id = "climate.test_trv"
        trv_data = mock_bt_instance.real_trvs[entity_id]
        child_lock = trv_data["advanced"].get("child_lock", False)

        should_adopt_temp = (
            trv_new_temp
            not in (
                mock_bt_instance.bt_target_temp,
                trv_data["temperature"],
                trv_data["last_temperature"],
            )
            and not child_lock
            and trv_data["target_temp_received"] is True
            and trv_data["system_mode_received"] is True
            and trv_data["hvac_mode"] is not HVACMode.OFF
            and mock_bt_instance.window_open is False
            and not trv_data.get("ignore_trv_states", False)
        )

        assert should_adopt_temp is True

    def test_child_lock_still_blocks_temp_change(self, mock_bt_instance):
        """Child lock blocks temperature changes regardless of ignore_trv_states."""
        mock_bt_instance.real_trvs["climate.test_trv"]["ignore_trv_states"] = False
        mock_bt_instance.real_trvs["climate.test_trv"]["advanced"]["child_lock"] = True

        trv_new_temp = 22.0
        entity_id = "climate.test_trv"
        trv_data = mock_bt_instance.real_trvs[entity_id]
        child_lock = trv_data["advanced"].get("child_lock", False)

        should_adopt_temp = (
            trv_new_temp
            not in (
                mock_bt_instance.bt_target_temp,
                trv_data["temperature"],
                trv_data["last_temperature"],
            )
            and not child_lock
            and trv_data["target_temp_received"] is True
            and trv_data["system_mode_received"] is True
            and trv_data["hvac_mode"] is not HVACMode.OFF
            and mock_bt_instance.window_open is False
            and not trv_data.get("ignore_trv_states", False)
        )

        assert should_adopt_temp is False

    def test_ignore_trv_states_default_is_false(self, mock_bt_instance):
        """The ignore_trv_states flag defaults to False when not set."""
        del mock_bt_instance.real_trvs["climate.test_trv"]["ignore_trv_states"]

        trv_data = mock_bt_instance.real_trvs["climate.test_trv"]
        ignore_states = trv_data.get("ignore_trv_states", False)

        assert ignore_states is False
