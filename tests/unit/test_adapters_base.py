"""Tests for adapters/base.py module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.adapters.base import (
    wait_for_calibration_entity_or_timeout,
)


class TestWaitForCalibrationEntityOrTimeout:
    """Test wait_for_calibration_entity_or_timeout function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.context = MagicMock()
        return mock

    @pytest.mark.anyio
    async def test_returns_early_when_calibration_entity_is_none(self, mock_self):
        """Test that function returns early when calibration_entity is None."""
        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.test_trv", None
        )
        # Should not attempt to get state if entity is None
        mock_self.hass.states.get.assert_not_called()

    @pytest.mark.anyio
    async def test_returns_when_entity_is_already_available(self, mock_self):
        """Test that function returns when entity is immediately available."""
        mock_state = MagicMock()
        mock_state.state = "1.5"
        mock_self.hass.states.get.return_value = mock_state

        await wait_for_calibration_entity_or_timeout(
            mock_self, "climate.test_trv", "number.test_calibration"
        )

        # Should check state once and return
        assert mock_self.hass.states.get.call_count == 1
        mock_self.hass.services.async_call.assert_not_called()

    @pytest.mark.anyio
    async def test_waits_and_retries_when_entity_unavailable(self, mock_self):
        """Test that function retries when entity is unavailable."""
        # First 2 calls return unavailable, 3rd call returns available
        mock_unavailable = MagicMock()
        mock_unavailable.state = "unavailable"
        mock_available = MagicMock()
        mock_available.state = "1.5"

        mock_self.hass.states.get.side_effect = [
            mock_unavailable,
            mock_unavailable,
            mock_available,
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test_trv", "number.test_calibration"
            )

        # Should retry twice (sleep called twice)
        assert mock_sleep.call_count == 2
        assert mock_self.hass.states.get.call_count == 3

    @pytest.mark.anyio
    async def test_forces_calibration_to_zero_after_timeout(self, mock_self):
        """Test that function forces calibration to 0 after max retries."""
        # Always return unavailable
        mock_unavailable = MagicMock()
        mock_unavailable.state = "unavailable"
        mock_self.hass.states.get.return_value = mock_unavailable

        mock_self.hass.services.async_call = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test_trv", "number.test_calibration"
            )

        # Should attempt to force set calibration to 0
        mock_self.hass.services.async_call.assert_called_once_with(
            "number",
            "set_value",
            {"entity_id": "number.test_calibration", "value": 0},
            blocking=False,
            context=mock_self.context,
        )

    @pytest.mark.anyio
    async def test_handles_exception_when_setting_calibration_fails(self, mock_self):
        """Test that function handles exception when forcing calibration fails."""
        mock_unavailable = MagicMock()
        mock_unavailable.state = "unavailable"
        mock_self.hass.states.get.return_value = mock_unavailable

        mock_self.hass.services.async_call = AsyncMock(
            side_effect=Exception("Service call failed")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Should not raise exception
            await wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test_trv", "number.test_calibration"
            )

    @pytest.mark.anyio
    async def test_handles_none_state(self, mock_self):
        """Test that function handles None state."""
        mock_self.hass.states.get.return_value = None

        mock_self.hass.services.async_call = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test_trv", "number.test_calibration"
            )

        # Should attempt to force set after timeout
        assert mock_self.hass.services.async_call.called

    @pytest.mark.anyio
    async def test_handles_unknown_state(self, mock_self):
        """Test that function treats 'unknown' state as unavailable."""
        mock_unknown = MagicMock()
        mock_unknown.state = "unknown"
        mock_available = MagicMock()
        mock_available.state = "2.0"

        mock_self.hass.states.get.side_effect = [mock_unknown, mock_available]

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test_trv", "number.test_calibration"
            )

        # Should retry once
        assert mock_sleep.call_count == 1

    @pytest.mark.anyio
    async def test_max_retries_is_six(self, mock_self):
        """Test that function retries exactly 6 times before timeout."""
        mock_unavailable = MagicMock()
        mock_unavailable.state = "unavailable"
        mock_self.hass.states.get.return_value = mock_unavailable
        mock_self.hass.services.async_call = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test_trv", "number.test_calibration"
            )

        # Should sleep 5 times (6 total attempts: 1 initial + 5 retries)
        assert mock_sleep.call_count == 5
        # Each sleep should be 5 seconds
        for call in mock_sleep.call_args_list:
            assert call[0][0] == 5