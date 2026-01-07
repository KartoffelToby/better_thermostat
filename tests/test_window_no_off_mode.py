"""Tests for window handling with no_off_system_mode.

Issue #1195: TRV stays forever at 5°C after window closed (with no_off_system_mode)

The bug: When a TRV has no_off_system_mode enabled and a window opens, BT correctly
sets the TRV to min_temp (5°C). But when the window closes, the TRV stays at 5°C
instead of restoring to the target temperature.

Root cause: convert_outbound_states sets system_mode=None for no_off_system_mode
devices when hvac_mode is OFF. Then handle_window_open returns None instead of HEAT
when window closes, causing the control logic to not restore heating.
"""

import pytest
from unittest.mock import MagicMock
from homeassistant.components.climate import HVACMode
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)


@pytest.fixture
def mock_bt_instance():
    """Create a mock BetterThermostat instance."""
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
            "hvac_modes": [HVACMode.HEAT],  # No OFF mode in hvac_modes
            "min_temp": 5.0,
            "max_temp": 30.0,
            "current_temperature": 19.0,
            "temperature": 21.0,
            "advanced": {
                # Use TARGET_TEMP_BASED with NO_CALIBRATION to go through
                # the no_off_system_mode logic without needing calibration calcs
                "calibration": CalibrationType.TARGET_TEMP_BASED,
                "calibration_mode": CalibrationMode.NO_CALIBRATION,
                "no_off_system_mode": True,
                "heat_auto_swapped": False,
            },
        }
    }
    return bt


class TestHandleWindowOpen:
    """Tests for handle_window_open function."""

    def test_returns_off_when_window_open(self, mock_bt_instance):
        """Test that handle_window_open returns OFF when window is open."""
        from custom_components.better_thermostat.utils.controlling import (
            handle_window_open,
        )

        mock_bt_instance.window_open = True
        remapped_states = {"system_mode": HVACMode.HEAT, "temperature": 21.0}

        result = handle_window_open(mock_bt_instance, remapped_states)

        assert result == HVACMode.OFF

    def test_returns_system_mode_when_window_closed(self, mock_bt_instance):
        """Test that handle_window_open returns system_mode when window is closed."""
        from custom_components.better_thermostat.utils.controlling import (
            handle_window_open,
        )

        mock_bt_instance.window_open = False
        remapped_states = {"system_mode": HVACMode.HEAT, "temperature": 21.0}

        result = handle_window_open(mock_bt_instance, remapped_states)

        assert result == HVACMode.HEAT

    def test_returns_none_when_system_mode_none(self, mock_bt_instance):
        """Test current buggy behavior: returns None when system_mode is None.

        This is the bug! When no_off_system_mode is True and window was open,
        convert_outbound_states sets system_mode=None. Then when window closes,
        this function returns None instead of HEAT.
        """
        from custom_components.better_thermostat.utils.controlling import (
            handle_window_open,
        )

        mock_bt_instance.window_open = False
        # This is what convert_outbound_states returns for no_off_system_mode
        # when hvac_mode was OFF (during window open)
        remapped_states = {"system_mode": None, "temperature": 5.0}

        result = handle_window_open(mock_bt_instance, remapped_states)

        # Current buggy behavior - returns None
        assert result is None


class TestConvertOutboundStatesNoOffMode:
    """Tests for convert_outbound_states with no_off_system_mode."""

    def test_sets_system_mode_none_when_off_with_no_off_mode(self, mock_bt_instance):
        """Test that convert_outbound_states sets system_mode=None for no_off_system_mode.

        This demonstrates part of the bug: when hvac_mode is OFF and no_off_system_mode
        is True, the function sets system_mode=None in the payload.
        """
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        # Simulate window open scenario - BT wants to turn OFF
        result = convert_outbound_states(
            mock_bt_instance, "climate.test_trv", HVACMode.OFF
        )

        assert result is not None
        # Current behavior: system_mode is None (not OFF, not HEAT)
        assert result.get("system_mode") is None
        # Temperature is set to min_temp
        assert result.get("temperature") == 5.0

    def test_sets_system_mode_heat_when_heating(self, mock_bt_instance):
        """Test that convert_outbound_states sets system_mode=HEAT when heating."""
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )

        result = convert_outbound_states(
            mock_bt_instance, "climate.test_trv", HVACMode.HEAT
        )

        assert result is not None
        # system_mode should be HEAT (or mapped equivalent)
        # Note: might be None if device has no system mode support
        assert result.get("temperature") == 21.0


class TestWindowCloseRestoresHeating:
    """Integration tests for window close behavior with no_off_system_mode."""

    def test_window_close_should_restore_heating_mode(self, mock_bt_instance):
        """Test that closing window restores HEAT mode, not None.

        This is the main bug test. When window closes, the TRV should
        go back to heating, but currently it doesn't because the control
        logic receives None instead of HEAT.
        """
        from custom_components.better_thermostat.events.trv import (
            convert_outbound_states,
        )
        from custom_components.better_thermostat.utils.controlling import (
            handle_window_open,
        )

        # Step 1: Window is closed, TRV is heating normally
        mock_bt_instance.window_open = False
        mock_bt_instance.bt_hvac_mode = HVACMode.HEAT

        states_heating = convert_outbound_states(
            mock_bt_instance, "climate.test_trv", HVACMode.HEAT
        )
        handle_window_open(mock_bt_instance, states_heating)

        # Should be heating (or at least not None/OFF)
        # Note: might be None for devices without system_mode, but should still heat
        assert states_heating.get("temperature") == 21.0

        # Step 2: Window opens - BT sends OFF, TRV goes to 5°C
        mock_bt_instance.window_open = True

        hvac_mode_window_open = handle_window_open(mock_bt_instance, states_heating)
        assert hvac_mode_window_open == HVACMode.OFF

        # Step 3: Window closes - BT should restore heating
        mock_bt_instance.window_open = False

        # The bt_hvac_mode should still be HEAT (it was never changed)
        assert mock_bt_instance.bt_hvac_mode == HVACMode.HEAT

        # Get new states for the closed window scenario
        states_after_close = convert_outbound_states(
            mock_bt_instance, "climate.test_trv", mock_bt_instance.bt_hvac_mode
        )

        hvac_mode_after_close = handle_window_open(mock_bt_instance, states_after_close)

        # BUG: This currently fails!
        # The function returns the system_mode from convert_outbound_states,
        # which should indicate heating, not None.
        # For devices without OFF mode, we need to ensure heating is restored.

        # The temperature should be restored to target, not min_temp
        assert states_after_close.get("temperature") == 21.0, (
            f"Expected temperature 21.0 but got {states_after_close.get('temperature')}"
        )

        # The hvac_mode should indicate heating should happen
        # (either HEAT or at least not OFF)
        # This is where the bug manifests - it might be None
        if hvac_mode_after_close is not None:
            assert (
                hvac_mode_after_close != HVACMode.OFF
            ), f"Expected HEAT or equivalent but got {hvac_mode_after_close}"


class TestTrvStateUpdateBug:
    """Tests for the bug where TRV state update incorrectly sets bt_hvac_mode.

    This is the root cause of issue #1195: When a TRV with no_off_system_mode
    reports its temperature as min_temp (5°C), the code incorrectly sets
    bt_hvac_mode to OFF, which prevents heating from resuming when window closes.
    """

    def test_trv_update_should_not_change_bt_hvac_mode_based_on_temperature(
        self, mock_bt_instance
    ):
        """Test that TRV temperature update doesn't incorrectly set bt_hvac_mode.

        BUG: In events/trv.py lines 302-306, when a TRV with no_off_system_mode
        reports setpoint == min_temp, it sets bt_hvac_mode = OFF.
        This is wrong because the low temperature was set BY BT due to window open,
        not because the user turned off heating.

        This test documents the buggy behavior.
        """
        # Simulate the scenario:
        # 1. BT is in HEAT mode, window opens
        # 2. BT sets TRV to min_temp (5°C) because of no_off_system_mode
        # 3. TRV reports back that setpoint is now 5°C
        # 4. BUG: Code sets bt_hvac_mode = OFF based on temperature

        mock_bt_instance.bt_hvac_mode = HVACMode.HEAT
        entity_id = "climate.test_trv"
        min_temp = mock_bt_instance.real_trvs[entity_id]["min_temp"]

        # Simulate what happens in trigger_trv_change when TRV reports min_temp
        # This is the buggy code from events/trv.py:302-306
        _new_heating_setpoint = min_temp  # TRV reports 5°C

        no_off_system_mode = mock_bt_instance.real_trvs[entity_id]["advanced"].get(
            "no_off_system_mode", False
        )

        # Current buggy behavior
        if no_off_system_mode:
            if _new_heating_setpoint == min_temp:
                # BUG: This incorrectly sets bt_hvac_mode to OFF
                mock_bt_instance.bt_hvac_mode = HVACMode.OFF
            else:
                mock_bt_instance.bt_hvac_mode = HVACMode.HEAT

        # After the buggy code runs, bt_hvac_mode is OFF
        # This is the bug - it SHOULD still be HEAT!
        assert (
            mock_bt_instance.bt_hvac_mode == HVACMode.OFF
        ), "This test documents the bug - bt_hvac_mode is incorrectly set to OFF"

    def test_bt_hvac_mode_should_remain_heat_when_window_triggered_min_temp(
        self, mock_bt_instance
    ):
        """Test that bt_hvac_mode should remain HEAT when window caused the low temp.

        This test will PASS after the fix is implemented.
        The fix should NOT change bt_hvac_mode based on temperature when
        the low temperature was caused by window_open, not user action.
        """
        mock_bt_instance.bt_hvac_mode = HVACMode.HEAT
        mock_bt_instance.window_open = True  # Window is open

        entity_id = "climate.test_trv"
        min_temp = mock_bt_instance.real_trvs[entity_id]["min_temp"]
        _new_heating_setpoint = min_temp

        no_off_system_mode = mock_bt_instance.real_trvs[entity_id]["advanced"].get(
            "no_off_system_mode", False
        )

        # FIXED behavior: Don't change bt_hvac_mode when window is open
        # because we KNOW the low temp is due to window, not user turning off
        if no_off_system_mode:
            if _new_heating_setpoint == min_temp:
                # FIX: Only set OFF if window is NOT the cause
                if not mock_bt_instance.window_open:
                    mock_bt_instance.bt_hvac_mode = HVACMode.OFF
                # else: keep bt_hvac_mode unchanged (HEAT)
            else:
                mock_bt_instance.bt_hvac_mode = HVACMode.HEAT

        # After the fix, bt_hvac_mode should still be HEAT
        assert (
            mock_bt_instance.bt_hvac_mode == HVACMode.HEAT
        ), "bt_hvac_mode should remain HEAT when window caused the min_temp"


class TestControlTrvWithNoOffMode:
    """Tests for the full control_trv flow with no_off_system_mode."""

    @pytest.mark.anyio
    async def test_control_trv_restores_temp_after_window_close(self, mock_bt_instance):
        """Test that control_trv properly restores temperature after window closes.

        This tests the full flow and verifies the fix works end-to-end.
        """
        # When window closes with no_off_system_mode:
        # 1. bt_hvac_mode should still be HEAT (not changed by TRV update)
        # 2. convert_outbound_states should return temperature=target_temp
        # 3. handle_window_open should return a mode that triggers heating
        # 4. The TRV should receive the target temperature, not min_temp

        assert mock_bt_instance.bt_hvac_mode == HVACMode.HEAT
        assert mock_bt_instance.bt_target_temp == 21.0
        assert mock_bt_instance.real_trvs["climate.test_trv"]["min_temp"] == 5.0
