"""Tests for the TRVZB setpoint and HVAC mode override quirks."""

import importlib
from unittest.mock import AsyncMock, Mock

import pytest

quirk = importlib.import_module("custom_components.better_thermostat.model_fixes.TRVZB")


def _make_self():
    """Create a mock BetterThermostat with a spied service-call layer."""
    mock_self = Mock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = Mock()
    mock_self.hass.services.async_call = AsyncMock()
    return mock_self


class TestOverrideSetTemperature:
    """The quirk declines so the generic adapter performs the write."""

    @pytest.mark.asyncio
    async def test_returns_false_without_service_call(self):
        """The override returns False and issues no service call."""
        mock_self = _make_self()

        handled = await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()


class TestOverrideSetHvacMode:
    """The quirk declines so the generic adapter performs the write."""

    @pytest.mark.asyncio
    async def test_returns_false_without_service_call(self):
        """The override returns False and issues no service call."""
        mock_self = _make_self()

        handled = await quirk.override_set_hvac_mode(mock_self, "climate.trv1", "heat")

        assert handled is False
        mock_self.hass.services.async_call.assert_not_awaited()
