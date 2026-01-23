"""Tests for control_cooler function in utils/controlling.py."""

from unittest.mock import AsyncMock, Mock

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.utils.controlling import control_cooler


class TestControlCooler:
    """Test control_cooler function."""

    @pytest.mark.asyncio
    async def test_off_mode_turns_cooler_off(self):
        """Test that OFF mode turns the cooler off."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.OFF
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None

        await control_cooler(mock_self)

        # Should only call set_hvac_mode to OFF
        mock_hass.services.async_call.assert_called_once_with(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.cooler", "hvac_mode": HVACMode.OFF},
            blocking=True,
            context=None,
        )

    @pytest.mark.asyncio
    async def test_cooling_needed_above_target(self):
        """Test cooling turns on when temp >= target_cooltemp - tolerance."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 25.0
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to COOL
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        # First call: set_temperature
        assert calls[0].args[0] == "climate"
        assert calls[0].args[1] == "set_temperature"
        assert calls[0].args[2]["entity_id"] == "climate.cooler"
        assert calls[0].args[2]["temperature"] == 24.0

        # Second call: set_hvac_mode to COOL
        assert calls[1].args[0] == "climate"
        assert calls[1].args[1] == "set_hvac_mode"
        assert calls[1].args[2]["hvac_mode"] == HVACMode.COOL

    @pytest.mark.asyncio
    async def test_cooling_not_needed_when_temp_below_bt_target(self):
        """Test cooling doesn't turn on if cur_temp <= bt_target_temp."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 20.0  # Equal to bt_target_temp
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to OFF (else branch)
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        assert calls[1][1]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_stop_cooling_below_threshold(self):
        """Test cooling stops when temp <= target_cooltemp - tolerance."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 23.0  # Below target_cooltemp - tolerance
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        # cur_temp (23.0) <= bt_target_cooltemp (24.0) - tolerance (0.5) = 23.5

        await control_cooler(mock_self)

        # Should call set_temperature and set_hvac_mode to OFF
        assert mock_hass.services.async_call.call_count == 2

        calls = mock_hass.services.async_call.call_args_list
        assert calls[1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_hysteresis_behavior(self):
        """Test hysteresis behavior between cooling thresholds."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        # Test temperature in hysteresis zone
        # cur_temp between (target_cooltemp - tolerance) and target_cooltemp
        # Should go to else branch and turn OFF
        mock_self.cur_temp = 23.7  # Between 23.5 and 24.0

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # Should set mode to OFF (else branch)
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_context_passed_to_service_calls(self):
        """Test that context is properly passed to service calls."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_context = Mock()
        mock_context.id = "test_context_id"

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.OFF
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = mock_context

        await control_cooler(mock_self)

        # Verify context was passed
        call_kwargs = mock_hass.services.async_call.call_args[1]
        assert call_kwargs["context"] == mock_context

    @pytest.mark.asyncio
    async def test_blocking_true_for_all_calls(self):
        """Test that all service calls use blocking=True."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.cur_temp = 25.0
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        await control_cooler(mock_self)

        # All calls should have blocking=True
        for call in mock_hass.services.async_call.call_args_list:
            assert call[1]["blocking"] is True

    @pytest.mark.asyncio
    async def test_edge_case_exactly_at_threshold(self):
        """Test behavior when temperature is exactly at threshold."""
        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.bt_hvac_mode = HVACMode.COOL
        mock_self.cooler_entity_id = "climate.cooler"
        mock_self.context = None
        mock_self.bt_target_cooltemp = 24.0
        mock_self.bt_target_temp = 20.0
        mock_self.tolerance = 0.5

        # Exactly at target_cooltemp - tolerance
        mock_self.cur_temp = 23.5

        await control_cooler(mock_self)

        calls = mock_hass.services.async_call.call_args_list
        # cur_temp (23.5) <= 23.5, so should turn OFF
        assert calls[-1].args[2]["hvac_mode"] == HVACMode.OFF
