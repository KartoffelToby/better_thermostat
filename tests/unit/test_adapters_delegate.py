"""Tests for adapters/delegate.py module.

Tests the delegate adapter functions including load_adapter, set_temperature
with rounding and clamping, and set_valve with override handling.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.adapters import delegate


class TestLoadAdapter:
    """Test load_adapter function."""

    @pytest.mark.asyncio
    async def test_load_adapter_success(self):
        """Test loading a specific adapter successfully."""
        mock_hass = Mock()
        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_device"
        mock_self.adapter = None

        mock_adapter_module = Mock()

        with patch(
            "homeassistant.helpers.importlib.async_import_module",
            return_value=mock_adapter_module,
        ) as mock_import:
            result = await delegate.load_adapter(
                mock_self, "zigbee2mqtt", "climate.trv1"
            )

            mock_import.assert_called_once_with(
                mock_hass,
                "custom_components.better_thermostat.adapters.zigbee2mqtt",
            )
            assert result == mock_adapter_module
            assert mock_self.adapter == mock_adapter_module

    @pytest.mark.asyncio
    async def test_load_adapter_generic_thermostat_renamed(self):
        """Test that generic_thermostat is renamed to generic."""
        mock_hass = Mock()
        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_device"

        mock_adapter_module = Mock()

        with patch(
            "homeassistant.helpers.importlib.async_import_module",
            return_value=mock_adapter_module,
        ) as mock_import:
            await delegate.load_adapter(
                mock_self, "generic_thermostat", "climate.trv1"
            )

            # Should import generic, not generic_thermostat
            mock_import.assert_called_once_with(
                mock_hass, "custom_components.better_thermostat.adapters.generic"
            )

    @pytest.mark.asyncio
    async def test_load_adapter_fallback_to_generic(self):
        """Test fallback to generic adapter when specific adapter fails."""
        mock_hass = Mock()
        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_device"

        mock_generic_module = Mock()

        with patch(
            "homeassistant.helpers.importlib.async_import_module",
            side_effect=[Exception("Not found"), mock_generic_module],
        ) as mock_import:
            result = await delegate.load_adapter(
                mock_self, "unknown_integration", "climate.trv1"
            )

            # Should try unknown first, then fallback to generic
            assert mock_import.call_count == 2
            assert result == mock_generic_module


class TestSetTemperature:
    """Test set_temperature function with rounding and clamping."""

    @pytest.mark.asyncio
    async def test_set_temperature_basic(self):
        """Test basic temperature setting."""
        mock_adapter = Mock()
        mock_adapter.set_temperature = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = None
        mock_self.bt_target_temp_step = 0.5
        mock_self.real_trvs = {
            "climate.trv1": {
                "adapter": mock_adapter,
                "min_temp": 5.0,
                "max_temp": 30.0,
            }
        }

        await delegate.set_temperature(mock_self, "climate.trv1", 20.0)

        # Should call adapter with temperature
        mock_adapter.set_temperature.assert_called_once()
        call_args = mock_adapter.set_temperature.call_args[0]
        assert call_args[0] == mock_self
        assert call_args[1] == "climate.trv1"
        assert call_args[2] == 20.0

    @pytest.mark.asyncio
    async def test_set_temperature_rounding_by_step(self):
        """Test temperature is rounded by step."""
        mock_adapter = Mock()
        mock_adapter.set_temperature = AsyncMock()

        mock_state = Mock()
        mock_state.attributes = {"target_temp_step": 0.5}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.bt_target_temp_step = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "adapter": mock_adapter,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "target_temp_step": None,
            }
        }

        # 20.3 should round to 20.5 with step 0.5
        await delegate.set_temperature(mock_self, "climate.trv1", 20.3)

        call_args = mock_adapter.set_temperature.call_args[0]
        assert call_args[2] == 20.5

    @pytest.mark.asyncio
    async def test_set_temperature_clamping_to_min(self):
        """Test temperature is clamped to minimum."""
        mock_adapter = Mock()
        mock_adapter.set_temperature = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = None
        mock_self.bt_target_temp_step = 0.5
        mock_self.real_trvs = {
            "climate.trv1": {
                "adapter": mock_adapter,
                "min_temp": 5.0,
                "max_temp": 30.0,
            }
        }

        # 3.0 should clamp to 5.0
        await delegate.set_temperature(mock_self, "climate.trv1", 3.0)

        call_args = mock_adapter.set_temperature.call_args[0]
        assert call_args[2] == 5.0

    @pytest.mark.asyncio
    async def test_set_temperature_clamping_to_max(self):
        """Test temperature is clamped to maximum."""
        mock_adapter = Mock()
        mock_adapter.set_temperature = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = None
        mock_self.bt_target_temp_step = 0.5
        mock_self.real_trvs = {
            "climate.trv1": {
                "adapter": mock_adapter,
                "min_temp": 5.0,
                "max_temp": 30.0,
            }
        }

        # 35.0 should clamp to 30.0
        await delegate.set_temperature(mock_self, "climate.trv1", 35.0)

        call_args = mock_adapter.set_temperature.call_args[0]
        assert call_args[2] == 30.0

    @pytest.mark.asyncio
    async def test_set_temperature_updates_last_temperature(self):
        """Test that last_temperature is updated after setting."""
        mock_adapter = Mock()
        mock_adapter.set_temperature = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = None
        mock_self.bt_target_temp_step = 0.5
        mock_self.real_trvs = {
            "climate.trv1": {
                "adapter": mock_adapter,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "last_temperature": 0.0,
            }
        }

        await delegate.set_temperature(mock_self, "climate.trv1", 22.0)

        # last_temperature should be updated to 22.0
        assert mock_self.real_trvs["climate.trv1"]["last_temperature"] == 22.0

    @pytest.mark.asyncio
    async def test_set_temperature_per_trv_step_precedence(self):
        """Test that per-TRV step takes precedence over global config."""
        mock_adapter = Mock()
        mock_adapter.set_temperature = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = Mock()
        mock_self.hass.states.get.return_value = None
        mock_self.bt_target_temp_step = 0.5  # Global step
        mock_self.real_trvs = {
            "climate.trv1": {
                "adapter": mock_adapter,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "target_temp_step": 1.0,  # Per-TRV step should win
            }
        }

        # 20.3 should round to 20.0 with step 1.0
        await delegate.set_temperature(mock_self, "climate.trv1", 20.3)

        call_args = mock_adapter.set_temperature.call_args[0]
        assert call_args[2] == 20.0


class TestSetValve:
    """Test set_valve function with override handling."""

    @pytest.mark.asyncio
    async def test_set_valve_with_override(self):
        """Test set_valve using model quirks override."""
        mock_override = AsyncMock(return_value=True)
        mock_quirks = Mock()
        mock_quirks.override_set_valve = mock_override

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.real_trvs = {
            "climate.trv1": {
                "model_quirks": mock_quirks,
                "last_valve_percent": 0,
                "last_valve_method": "",
            }
        }

        result = await delegate.set_valve(mock_self, "climate.trv1", 75)

        # Should call override and return True
        mock_override.assert_called_once_with(mock_self, "climate.trv1", 75)
        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["last_valve_percent"] == 75
        assert mock_self.real_trvs["climate.trv1"]["last_valve_method"] == "override"

    @pytest.mark.asyncio
    async def test_set_valve_with_writable_entity(self):
        """Test set_valve using writable valve entity."""
        mock_adapter = Mock()
        mock_adapter.set_valve = AsyncMock()

        mock_quirks = Mock()
        mock_quirks.override_set_valve = None

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.real_trvs = {
            "climate.trv1": {
                "model_quirks": mock_quirks,
                "valve_position_entity": "number.valve",
                "valve_position_writable": True,
                "adapter": mock_adapter,
                "last_valve_percent": 0,
                "last_valve_method": "",
            }
        }

        result = await delegate.set_valve(mock_self, "climate.trv1", 50)

        # Should call adapter and return True
        mock_adapter.set_valve.assert_called_once_with(mock_self, "climate.trv1", 50)
        assert result is True
        assert mock_self.real_trvs["climate.trv1"]["last_valve_percent"] == 50
        assert mock_self.real_trvs["climate.trv1"]["last_valve_method"] == "adapter"

    @pytest.mark.asyncio
    async def test_set_valve_not_writable(self):
        """Test set_valve when valve entity is not writable."""
        mock_quirks = Mock()
        mock_quirks.override_set_valve = None

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.real_trvs = {
            "climate.trv1": {
                "model_quirks": mock_quirks,
                "valve_position_entity": "sensor.valve",
                "valve_position_writable": False,  # Not writable
            }
        }

        result = await delegate.set_valve(mock_self, "climate.trv1", 50)

        # Should return False since not writable
        assert result is False

    @pytest.mark.asyncio
    async def test_set_valve_no_entity(self):
        """Test set_valve when no valve entity exists."""
        mock_quirks = Mock()
        mock_quirks.override_set_valve = None

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.real_trvs = {
            "climate.trv1": {
                "model_quirks": mock_quirks,
                "valve_position_entity": None,  # No entity
                "valve_position_writable": False,
            }
        }

        result = await delegate.set_valve(mock_self, "climate.trv1", 50)

        # Should return False
        assert result is False


class TestSetHvacMode:
    """Test set_hvac_mode function."""

    @pytest.mark.asyncio
    async def test_set_hvac_mode(self):
        """Test setting HVAC mode."""
        mock_adapter = Mock()
        mock_adapter.set_hvac_mode = AsyncMock(return_value=None)

        mock_self = Mock()
        mock_self.real_trvs = {"climate.trv1": {"adapter": mock_adapter}}

        await delegate.set_hvac_mode(mock_self, "climate.trv1", HVACMode.HEAT)

        mock_adapter.set_hvac_mode.assert_called_once_with(
            mock_self, "climate.trv1", HVACMode.HEAT
        )