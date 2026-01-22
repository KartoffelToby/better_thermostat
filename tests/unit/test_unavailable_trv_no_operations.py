"""Test that unavailable TRVs don't execute control operations.

This test verifies the fix for the incomplete PR #1813 implementation.

Problem: After PR #1813, the code logged "skipping control" for unavailable TRVs
but still executed all control operations (Lines 271-582):
- convert_outbound_states()
- set_valve()
- set_hvac_mode()
- set_offset()
- set_temperature()

Fix: Return True immediately after detecting unavailable TRV, without executing
any operations.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import STATE_UNAVAILABLE
import pytest

from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.controlling import control_trv


@pytest.mark.asyncio
async def test_unavailable_trv_no_operations_called():
    """Test that unavailable TRVs don't execute any control operations."""
    mock_state = Mock()
    mock_state.state = STATE_UNAVAILABLE

    mock_hass = Mock()
    mock_hass.states.get.return_value = mock_state

    mock_self = Mock()
    mock_self.hass = mock_hass
    mock_self.device_name = "test_thermostat"
    mock_self._temp_lock = asyncio.Lock()
    mock_self.calculate_heating_power = AsyncMock()
    mock_self.real_trvs = {
        "climate.trv1": {
            "ignore_trv_states": False,
            "advanced": {
                "calibration_mode": CalibrationMode.MPC_CALIBRATION,
            },
        }
    }

    with patch(
        "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
    ) as mock_convert, patch(
        "custom_components.better_thermostat.utils.controlling.set_valve"
    ) as mock_set_valve, patch(
        "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
    ) as mock_set_hvac_mode, patch(
        "custom_components.better_thermostat.utils.controlling.set_offset"
    ) as mock_set_offset, patch(
        "custom_components.better_thermostat.utils.controlling.set_temperature"
    ) as mock_set_temp:
        result = await control_trv(mock_self, "climate.trv1")

        assert result is True

        # Verify no operations were called
        mock_convert.assert_not_called()
        mock_set_valve.assert_not_called()
        mock_set_hvac_mode.assert_not_called()
        mock_set_offset.assert_not_called()
        mock_set_temp.assert_not_called()
