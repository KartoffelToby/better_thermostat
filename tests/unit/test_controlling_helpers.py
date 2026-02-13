"""Tests for helper functions in utils/controlling.py."""

import asyncio
from unittest.mock import Mock

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.controlling import (
    check_system_mode,
    check_target_temperature,
    handle_window_open,
)


class TestHandleWindowOpen:
    """Test handle_window_open function."""

    def test_window_open_returns_off(self):
        """Test that window open returns HVACMode.OFF."""
        # Create mock self object
        mock_self = Mock()
        mock_self.window_open = True

        remapped_states = {"system_mode": HVACMode.HEAT}

        result = handle_window_open(mock_self, remapped_states)

        assert result == HVACMode.OFF

    def test_window_closed_returns_system_mode(self):
        """Test that window closed returns system_mode from remapped_states."""
        mock_self = Mock()
        mock_self.window_open = False

        remapped_states = {"system_mode": HVACMode.HEAT}

        result = handle_window_open(mock_self, remapped_states)

        assert result == HVACMode.HEAT

    def test_window_closed_no_system_mode(self):
        """Test that window closed with no system_mode returns None."""
        mock_self = Mock()
        mock_self.window_open = False

        remapped_states = {}

        result = handle_window_open(mock_self, remapped_states)

        assert result is None

    def test_window_closed_system_mode_none(self):
        """Test that window closed with system_mode=None returns None."""
        mock_self = Mock()
        mock_self.window_open = False

        remapped_states = {"system_mode": None}

        result = handle_window_open(mock_self, remapped_states)

        assert result is None


class TestCheckSystemMode:
    """Test check_system_mode function."""

    @pytest.mark.asyncio
    async def test_mode_matches_immediately(self):
        """Test when mode matches immediately."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": {
                "hvac_mode": HVACMode.HEAT,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
            }
        }

        result = await check_system_mode(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["system_mode_received"] is True

    @pytest.mark.asyncio
    async def test_mode_matches_after_delay(self):
        """Test when mode matches after a short delay."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": {
                "hvac_mode": HVACMode.OFF,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
            }
        }

        # Simulate mode change after 0.5 seconds
        async def update_mode():
            await asyncio.sleep(0.1)
            mock_self.real_trvs["climate.trv1"]["hvac_mode"] = HVACMode.HEAT

        update_task = asyncio.create_task(update_mode())

        result = await check_system_mode(mock_self, "climate.trv1")

        await update_task
        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["system_mode_received"] is True

    @pytest.mark.asyncio
    async def test_timeout_after_360_seconds(self):
        """Test timeout after 360 seconds.

        Note: We use a shorter timeout for testing by mocking sleep.
        """
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": {
                "hvac_mode": HVACMode.OFF,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
            }
        }

        # Track sleep calls
        sleep_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(duration):
            nonlocal sleep_count
            if duration == 1:
                sleep_count += 1
                # Simulate 361 sleep calls quickly
                if sleep_count > 360:
                    return
            await original_sleep(0.001)  # Actually sleep very briefly

        # Patch asyncio.sleep
        import custom_components.better_thermostat.utils.controlling as controlling_module

        original_sleep_func = controlling_module.asyncio.sleep
        controlling_module.asyncio.sleep = mock_sleep

        try:
            result = await check_system_mode(mock_self, "climate.trv1")

            assert result is True
            # Flag should still be set to True after timeout
            assert mock_self.real_trvs["climate.trv1"]["system_mode_received"] is True
            # Mode should not have changed
            assert mock_self.real_trvs["climate.trv1"]["hvac_mode"] == HVACMode.OFF
        finally:
            controlling_module.asyncio.sleep = original_sleep_func

    @pytest.mark.asyncio
    async def test_system_mode_received_flag_set(self):
        """Test that system_mode_received flag is always set to True."""
        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.real_trvs = {
            "climate.trv1": {
                "hvac_mode": HVACMode.HEAT,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
            }
        }

        await check_system_mode(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"]["system_mode_received"] is True


class TestCheckTargetTemperature:
    """Test check_target_temperature function."""

    @pytest.mark.asyncio
    async def test_temperature_matches_immediately(self):
        """Test when temperature matches immediately."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": 21.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"last_temperature": 21.0, "target_temp_received": False}
        }

        result = await check_target_temperature(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["target_temp_received"] is True

    @pytest.mark.asyncio
    async def test_temperature_is_none(self):
        """Test when current temperature is None."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": None}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"last_temperature": 21.0, "target_temp_received": False}
        }

        result = await check_target_temperature(mock_self, "climate.trv1")

        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["target_temp_received"] is True

    @pytest.mark.asyncio
    async def test_temperature_matches_after_delay(self):
        """Test when temperature matches after a delay."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"last_temperature": 21.0, "target_temp_received": False}
        }

        # Simulate temperature change after 0.1 seconds
        async def update_temp():
            await asyncio.sleep(0.1)
            mock_state.attributes["temperature"] = 21.0

        update_task = asyncio.create_task(update_temp())

        result = await check_target_temperature(mock_self, "climate.trv1")

        await update_task
        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["target_temp_received"] is True

    @pytest.mark.asyncio
    async def test_timeout_after_360_seconds(self):
        """Test timeout after 360 seconds."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"last_temperature": 21.0, "target_temp_received": False}
        }

        # Track sleep calls
        sleep_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(duration):
            nonlocal sleep_count
            if duration == 1:
                sleep_count += 1
                if sleep_count > 360:
                    return
            await original_sleep(0.001)

        import custom_components.better_thermostat.utils.controlling as controlling_module

        original_sleep_func = controlling_module.asyncio.sleep
        controlling_module.asyncio.sleep = mock_sleep

        try:
            result = await check_target_temperature(mock_self, "climate.trv1")

            assert result is True
            assert mock_self.real_trvs["climate.trv1"]["target_temp_received"] is True
        finally:
            controlling_module.asyncio.sleep = original_sleep_func

    @pytest.mark.asyncio
    async def test_convert_to_float_called(self):
        """Test that convert_to_float is used for temperature conversion."""
        mock_state = Mock()
        mock_state.attributes = {"temperature": "21.0"}  # String value

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_thermostat"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"last_temperature": 21.0, "target_temp_received": False}
        }

        result = await check_target_temperature(mock_self, "climate.trv1")

        assert result is True
        # convert_to_float should handle string "21.0" and match float 21.0
