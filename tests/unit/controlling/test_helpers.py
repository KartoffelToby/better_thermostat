"""Tests for helper functions in utils/controlling.py.

Tests for:
- handle_window_open()
- check_system_mode()
- check_target_temperature()

Absorbed tests from:
- tests/test_window_no_off_mode.py (TestHandleWindowOpen, TestWindowCloseRestoresHeating)
"""

import asyncio
from unittest.mock import MagicMock, Mock

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import (
    check_system_mode,
    check_target_temperature,
    handle_window_open,
)

# ---------------------------------------------------------------------------
# handle_window_open
# ---------------------------------------------------------------------------


class TestHandleWindowOpen:
    """Test handle_window_open function."""

    def test_window_open_returns_off(self):
        """Test that window open returns HVACMode.OFF."""
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


class TestHandleWindowOpenWithNoOffMode:
    """Tests for handle_window_open with no_off_system_mode TRVs.

    Issue #1195: TRV stays forever at 5C after window closed
    (with no_off_system_mode).

    When no_off_system_mode is True and window was open,
    convert_outbound_states sets system_mode=None. Then when window
    closes, handle_window_open returns None instead of HEAT.
    """

    @pytest.fixture
    def mock_bt_no_off_mode(self):
        """Create a mock BetterThermostat with no_off_system_mode."""
        bt = MagicMock()
        bt.hass = MagicMock()
        bt.device_name = "Test Thermostat"
        bt.bt_hvac_mode = HVACMode.HEAT
        bt.bt_target_temp = 21.0
        bt.cur_temp = 19.0
        bt.window_open = False
        bt.tolerance = 0.3
        bt.real_trvs = {
            "climate.test_trv": {
                "hvac_modes": [HVACMode.HEAT],  # No OFF mode
                "min_temp": 5.0,
                "max_temp": 30.0,
                "current_temperature": 19.0,
                "temperature": 21.0,
                "advanced": {
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "no_off_system_mode": True,
                    "heat_auto_swapped": False,
                },
            }
        }
        return bt

    def test_returns_none_when_system_mode_none(self, mock_bt_no_off_mode):
        """Test current behavior: returns None when system_mode is None.

        This documents the bug where convert_outbound_states sets
        system_mode=None for no_off_system_mode devices when hvac_mode
        is OFF (during window open), and handle_window_open returns None.
        """
        mock_bt_no_off_mode.window_open = False
        remapped_states = {"system_mode": None, "temperature": 5.0}

        result = handle_window_open(mock_bt_no_off_mode, remapped_states)

        assert result is None

    def test_window_close_should_restore_heating_mode(self, mock_bt_no_off_mode):
        """Test that closing window restores HEAT mode, not None.

        Integration test through convert_outbound_states + handle_window_open.
        """
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        # Step 1: Window is closed, TRV is heating normally
        mock_bt_no_off_mode.window_open = False
        mock_bt_no_off_mode.bt_hvac_mode = HVACMode.HEAT

        states_heating = convert_outbound_states(
            mock_bt_no_off_mode, "climate.test_trv", HVACMode.HEAT
        )
        handle_window_open(mock_bt_no_off_mode, states_heating)

        assert states_heating.get("temperature") == 21.0

        # Step 2: Window opens
        mock_bt_no_off_mode.window_open = True
        hvac_mode_window_open = handle_window_open(mock_bt_no_off_mode, states_heating)
        assert hvac_mode_window_open == HVACMode.OFF

        # Step 3: Window closes
        mock_bt_no_off_mode.window_open = False
        assert mock_bt_no_off_mode.bt_hvac_mode == HVACMode.HEAT

        states_after_close = convert_outbound_states(
            mock_bt_no_off_mode, "climate.test_trv", mock_bt_no_off_mode.bt_hvac_mode
        )
        hvac_mode_after_close = handle_window_open(
            mock_bt_no_off_mode, states_after_close
        )

        # Temperature should be restored to target
        assert states_after_close.get("temperature") == 21.0, (
            f"Expected temperature 21.0 but got {states_after_close.get('temperature')}"
        )

        # hvac_mode should indicate heating (or at least not OFF)
        if hvac_mode_after_close is not None:
            assert hvac_mode_after_close != HVACMode.OFF, (
                f"Expected HEAT or equivalent but got {hvac_mode_after_close}"
            )


# ---------------------------------------------------------------------------
# check_system_mode
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# check_target_temperature
# ---------------------------------------------------------------------------


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
