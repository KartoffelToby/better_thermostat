"""Tests for adapters/base.py module.

Tests the wait_for_calibration_entity_or_timeout function which waits for
calibration entities to become available with timeout handling.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from custom_components.better_thermostat.adapters.base import (
    wait_for_calibration_entity_or_timeout,
)


class TestWaitForCalibrationEntity:
    """Test wait_for_calibration_entity_or_timeout function."""

    @pytest.mark.asyncio
    async def test_calibration_entity_none_returns_early(self):
        """Test that function returns early when calibration_entity is None."""
        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = Mock()

        # Should return without error
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", None
        )

        # Hass should not be called since we return early
        mock_self.hass.states.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_calibration_entity_available_immediately(self):
        """Test when calibration entity is available immediately."""
        mock_state = Mock()
        mock_state.state = "0.0"

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass

        # Should return quickly without retries
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", "number.calibration"
        )

        # Should only check once since it's available
        assert mock_hass.states.get.call_count == 1

    @pytest.mark.asyncio
    async def test_calibration_entity_becomes_available_after_retry(self):
        """Test when calibration entity becomes available after retry."""
        # First call: unavailable, second call: available
        mock_state_unavail = Mock()
        mock_state_unavail.state = STATE_UNAVAILABLE

        mock_state_avail = Mock()
        mock_state_avail.state = "0.0"

        mock_hass = Mock()
        mock_hass.states.get.side_effect = [mock_state_unavail, mock_state_avail]

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass

        # Should return after second check
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", "number.calibration"
        )

        # Should check twice
        assert mock_hass.states.get.call_count == 2

    @pytest.mark.asyncio
    async def test_calibration_entity_timeout_forces_zero(self):
        """Test that timeout forces calibration to 0."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None

        # Should timeout after 6 retries (30 seconds)
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", "number.calibration"
        )

        # Should call service to set calibration to 0
        mock_hass.services.async_call.assert_called_once()
        call_args = mock_hass.services.async_call.call_args
        assert call_args[0][0] == "number"
        assert call_args[0][1] == "set_value"
        assert call_args[0][2]["entity_id"] == "number.calibration"
        assert call_args[0][2]["value"] == 0

    @pytest.mark.asyncio
    async def test_calibration_entity_unknown_state(self):
        """Test when calibration entity has unknown state."""
        mock_state = Mock()
        mock_state.state = STATE_UNKNOWN

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None

        # Should timeout and force to 0
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", "number.calibration"
        )

        # Should call service to set calibration to 0
        mock_hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_calibration_entity_none_state_object(self):
        """Test when state object is None."""
        mock_hass = Mock()
        mock_hass.states.get.return_value = None
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None

        # Should timeout and force to 0
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", "number.calibration"
        )

        # Should call service to set calibration to 0
        mock_hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_service_call_exception_handled(self):
        """Test that exceptions during service call are handled gracefully."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock(
            side_effect=Exception("Service error")
        )

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None

        # Should not raise exception
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.trv1", "number.calibration"
        )

        # Should have attempted the call
        mock_hass.services.async_call.assert_called_once()