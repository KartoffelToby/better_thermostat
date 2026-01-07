"""Tests for convert_outbound_states fallback mode.

Issue #1698: When calibration_type is None, the system_mode logic is skipped,
causing the TRV to receive incorrect hvac_mode values.

The bug: In convert_outbound_states(), when _calibration_type is None (fallback mode),
the entire system_mode processing block (mode_remap, no_off_system_mode handling, etc.)
is inside the `else` branch and gets skipped. This means:
- hvac_mode is not remapped (e.g., heat -> auto for some devices)
- no_off_system_mode logic is ignored
- Devices without OFF mode are not handled correctly
"""

import pytest
from unittest.mock import MagicMock
from homeassistant.components.climate import HVACMode


@pytest.fixture
def mock_bt_instance_no_calibration():
    """Create a mock BetterThermostat instance without calibration configured."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 21.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.real_trvs = {
        "climate.test_trv": {
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "current_temperature": 20.0,
            "temperature": 21.0,
            "advanced": {
                # No calibration type set - triggers fallback mode
                "calibration": None,
                "calibration_mode": None,
                "no_off_system_mode": False,
                "heat_auto_swapped": False,
            },
        }
    }
    return bt


@pytest.fixture
def mock_bt_instance_no_calibration_with_remap():
    """Create a mock BT instance that needs heat->auto remapping."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat Remap"
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.bt_target_temp = 21.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.real_trvs = {
        "climate.test_trv_remap": {
            "hvac_modes": [HVACMode.AUTO, HVACMode.OFF],  # No HEAT mode, needs remap
            "min_temp": 5.0,
            "max_temp": 30.0,
            "current_temperature": 20.0,
            "temperature": 21.0,
            "advanced": {
                "calibration": None,  # Fallback mode
                "calibration_mode": None,
                "no_off_system_mode": False,
                "heat_auto_swapped": True,  # heat should be remapped to auto
            },
        }
    }
    return bt


@pytest.fixture
def mock_bt_instance_no_calibration_no_off():
    """Create a mock BT instance with no_off_system_mode in fallback mode."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat No Off"
    bt.bt_hvac_mode = HVACMode.OFF
    bt.bt_target_temp = 21.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.real_trvs = {
        "climate.test_trv_no_off": {
            "hvac_modes": [HVACMode.HEAT],  # No OFF mode
            "min_temp": 5.0,
            "max_temp": 30.0,
            "current_temperature": 20.0,
            "temperature": 21.0,
            "advanced": {
                "calibration": None,  # Fallback mode
                "calibration_mode": None,
                "no_off_system_mode": True,  # Device has no OFF system mode
                "heat_auto_swapped": False,
            },
        }
    }
    return bt


class TestFallbackModeSystemMode:
    """Tests for system_mode handling in fallback mode."""

    def test_fallback_mode_returns_payload(self, mock_bt_instance_no_calibration):
        """Test that fallback mode returns a valid payload."""
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        result = convert_outbound_states(
            mock_bt_instance_no_calibration, "climate.test_trv", HVACMode.HEAT
        )

        assert result is not None
        assert "temperature" in result
        assert "system_mode" in result
        assert result["temperature"] == 21.0

    def test_fallback_mode_remaps_hvac_mode(
        self, mock_bt_instance_no_calibration_with_remap
    ):
        """Test that hvac_mode is remapped even in fallback mode.

        BUG: Currently the mode_remap is skipped in fallback mode,
        so heat is not converted to auto even when heat_auto_swapped is True.
        """
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        result = convert_outbound_states(
            mock_bt_instance_no_calibration_with_remap,
            "climate.test_trv_remap",
            HVACMode.HEAT,
        )

        assert result is not None
        # With heat_auto_swapped=True, HEAT should be remapped to AUTO
        # BUG: Currently returns HEAT because remap is skipped in fallback mode
        assert result["system_mode"] == HVACMode.AUTO, (
            f"Expected AUTO but got {result['system_mode']} - "
            "mode_remap is being skipped in fallback mode"
        )

    def test_fallback_mode_handles_no_off_system_mode(
        self, mock_bt_instance_no_calibration_no_off
    ):
        """Test that no_off_system_mode is handled in fallback mode.

        BUG: Currently the no_off_system_mode logic is skipped in fallback mode,
        so when hvac_mode is OFF on a device without OFF support, it's sent as-is
        instead of setting min_temp and system_mode=None.
        """
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        result = convert_outbound_states(
            mock_bt_instance_no_calibration_no_off,
            "climate.test_trv_no_off",
            HVACMode.OFF,
        )

        assert result is not None
        # With no_off_system_mode=True and hvac_mode=OFF:
        # - temperature should be set to min_temp (5.0)
        # - system_mode should be None (not OFF)
        # BUG: Currently sends OFF and target_temp instead
        assert result["temperature"] == 5.0, (
            f"Expected min_temp 5.0 but got {result['temperature']} - "
            "no_off_system_mode logic is being skipped in fallback mode"
        )
        assert result["system_mode"] is None, (
            f"Expected None but got {result['system_mode']} - "
            "no_off_system_mode logic is being skipped in fallback mode"
        )


class TestFallbackModeTemperature:
    """Tests for temperature handling in fallback mode."""

    def test_fallback_mode_uses_target_temp(self, mock_bt_instance_no_calibration):
        """Test that fallback mode correctly uses bt_target_temp."""
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        mock_bt_instance_no_calibration.bt_target_temp = 22.5

        result = convert_outbound_states(
            mock_bt_instance_no_calibration, "climate.test_trv", HVACMode.HEAT
        )

        assert result is not None
        assert result["temperature"] == 22.5

    def test_fallback_mode_no_calibration_offset(self, mock_bt_instance_no_calibration):
        """Test that fallback mode does not include calibration offset."""
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        result = convert_outbound_states(
            mock_bt_instance_no_calibration, "climate.test_trv", HVACMode.HEAT
        )

        assert result is not None
        # Fallback mode should not include calibration
        assert "local_temperature_calibration" not in result
