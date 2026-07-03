"""Tests for the BTH-RM230Z set_temperature model quirk."""

import importlib
from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate.const import ClimateEntityFeature
import pytest

quirk = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.BTH-RM230Z"
)

RANGE_BIT = int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)


def _make_self(state):
    """Create a mock BetterThermostat whose TRV state lookup returns state."""
    mock_self = Mock()
    mock_self.device_name = "test_thermostat"
    mock_self.context = Mock()
    mock_self.hass.states.get.return_value = state
    mock_self.hass.services.async_call = AsyncMock()
    return mock_self


def _state(supported_features):
    """Create a mock climate state with the given supported_features."""
    state = Mock()
    state.attributes = {"supported_features": supported_features}
    return state


class TestOverrideSetTemperature:
    """The quirk picks the write attributes from the live feature bitmask."""

    @pytest.mark.asyncio
    async def test_range_supported_writes_high_and_low(self):
        """With the range feature active, both range attributes are written."""
        mock_self = _make_self(_state(RANGE_BIT))

        handled = await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)

        assert handled is True
        mock_self.hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {
                "entity_id": "climate.trv1",
                "target_temp_high": 21.0,
                "target_temp_low": 21.0,
            },
            blocking=True,
            context=mock_self.context,
        )

    @pytest.mark.asyncio
    async def test_no_range_support_writes_single_setpoint(self):
        """Without the range feature, the plain temperature attribute is written."""
        mock_self = _make_self(_state(0))

        handled = await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)

        assert handled is True
        mock_self.hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {"entity_id": "climate.trv1", "temperature": 21.0},
            blocking=True,
            context=mock_self.context,
        )

    @pytest.mark.asyncio
    async def test_missing_state_falls_back_to_single_setpoint(self):
        """Without a current state, the quirk falls back to a plain write."""
        mock_self = _make_self(None)

        handled = await quirk.override_set_temperature(mock_self, "climate.trv1", 21.0)

        assert handled is True
        mock_self.hass.services.async_call.assert_awaited_once_with(
            "climate",
            "set_temperature",
            {"entity_id": "climate.trv1", "temperature": 21.0},
            blocking=True,
            context=mock_self.context,
        )
