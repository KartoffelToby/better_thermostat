"""Tests for device_condition.py module."""

from unittest.mock import MagicMock

import pytest
from homeassistant.components.climate.const import HVACAction, HVACMode

from custom_components.better_thermostat.device_condition import (
    async_condition_from_config,
    async_get_condition_capabilities,
    async_get_conditions,
)


class TestAsyncGetConditions:
    """Test async_get_conditions function."""

    @pytest.mark.anyio
    async def test_returns_conditions_for_better_thermostat_device(self):
        """Test that conditions are returned for Better Thermostat device."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        # Create mock entities for the device
        mock_entry1 = MagicMock()
        mock_entry1.domain = "better_thermostat"
        mock_entry1.entity_id = "climate.test_thermostat"

        mock_entry2 = MagicMock()
        mock_entry2.domain = "sensor"
        mock_entry2.entity_id = "sensor.other_sensor"

        mock_registry.entities.values.return_value = [mock_entry1, mock_entry2]

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_condition.entity_registry.async_get",
            return_value=mock_registry,
        ):
            with pytest.mock.patch(
                "custom_components.better_thermostat.device_condition.entity_registry.async_entries_for_device",
                return_value=[mock_entry1, mock_entry2],
            ):
                conditions = await async_get_conditions(mock_hass, "device_123")

        # Should return 2 conditions (is_hvac_mode and is_hvac_action) for better_thermostat entity
        assert len(conditions) == 2
        assert any(c["type"] == "is_hvac_mode" for c in conditions)
        assert any(c["type"] == "is_hvac_action" for c in conditions)

    @pytest.mark.anyio
    async def test_returns_empty_list_when_no_better_thermostat_entities(self):
        """Test that empty list is returned when no better_thermostat entities."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        # Only non-better_thermostat entities
        mock_entry = MagicMock()
        mock_entry.domain = "sensor"
        mock_entry.entity_id = "sensor.other_sensor"

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_condition.entity_registry.async_get",
            return_value=mock_registry,
        ):
            with pytest.mock.patch(
                "custom_components.better_thermostat.device_condition.entity_registry.async_entries_for_device",
                return_value=[mock_entry],
            ):
                conditions = await async_get_conditions(mock_hass, "device_123")

        assert len(conditions) == 0


class TestAsyncConditionFromConfig:
    """Test async_condition_from_config function."""

    def test_creates_hvac_mode_condition_checker(self):
        """Test that HVAC mode condition checker is created correctly."""
        mock_hass = MagicMock()
        config = {
            "type": "is_hvac_mode",
            "entity_id": "climate.test_thermostat",
            "hvac_mode": HVACMode.HEAT,
        }

        checker = async_condition_from_config(mock_hass, config)

        # Test the condition checker
        mock_state = MagicMock()
        mock_state.attributes = {"hvac_mode": HVACMode.HEAT}
        mock_hass.states.get.return_value = mock_state

        assert checker(mock_hass, None) is True

    def test_hvac_mode_condition_returns_false_when_mode_mismatch(self):
        """Test that HVAC mode condition returns False when mode doesn't match."""
        mock_hass = MagicMock()
        config = {
            "type": "is_hvac_mode",
            "entity_id": "climate.test_thermostat",
            "hvac_mode": HVACMode.HEAT,
        }

        checker = async_condition_from_config(mock_hass, config)

        mock_state = MagicMock()
        mock_state.attributes = {"hvac_mode": HVACMode.OFF}
        mock_hass.states.get.return_value = mock_state

        assert checker(mock_hass, None) is False

    def test_hvac_mode_condition_returns_false_when_state_is_none(self):
        """Test that HVAC mode condition returns False when state is None."""
        mock_hass = MagicMock()
        config = {
            "type": "is_hvac_mode",
            "entity_id": "climate.test_thermostat",
            "hvac_mode": HVACMode.HEAT,
        }

        checker = async_condition_from_config(mock_hass, config)

        mock_hass.states.get.return_value = None

        assert checker(mock_hass, None) is False

    def test_creates_hvac_action_condition_checker(self):
        """Test that HVAC action condition checker is created correctly."""
        mock_hass = MagicMock()
        config = {
            "type": "is_hvac_action",
            "entity_id": "climate.test_thermostat",
            "hvac_action": HVACAction.HEATING,
        }

        checker = async_condition_from_config(mock_hass, config)

        mock_state = MagicMock()
        mock_state.attributes = {"hvac_action": HVACAction.HEATING}
        mock_hass.states.get.return_value = mock_state

        assert checker(mock_hass, None) is True

    def test_hvac_action_condition_returns_false_when_action_mismatch(self):
        """Test that HVAC action condition returns False when action doesn't match."""
        mock_hass = MagicMock()
        config = {
            "type": "is_hvac_action",
            "entity_id": "climate.test_thermostat",
            "hvac_action": HVACAction.HEATING,
        }

        checker = async_condition_from_config(mock_hass, config)

        mock_state = MagicMock()
        mock_state.attributes = {"hvac_action": HVACAction.IDLE}
        mock_hass.states.get.return_value = mock_state

        assert checker(mock_hass, None) is False

    def test_returns_false_function_for_unknown_type(self):
        """Test that a function returning False is returned for unknown type."""
        mock_hass = MagicMock()
        config = {
            "type": "unknown_type",
            "entity_id": "climate.test_thermostat",
        }

        checker = async_condition_from_config(mock_hass, config)

        assert checker(mock_hass, None) is False


class TestAsyncGetConditionCapabilities:
    """Test async_get_condition_capabilities function."""

    @pytest.mark.anyio
    async def test_returns_hvac_mode_capabilities(self):
        """Test that HVAC mode capabilities are returned."""
        mock_hass = MagicMock()
        config = {"type": "is_hvac_mode"}

        capabilities = await async_get_condition_capabilities(mock_hass, config)

        assert "extra_fields" in capabilities
        # Should have hvac_mode field
        assert capabilities["extra_fields"] is not None

    @pytest.mark.anyio
    async def test_returns_hvac_action_capabilities(self):
        """Test that HVAC action capabilities are returned."""
        mock_hass = MagicMock()
        config = {"type": "is_hvac_action"}

        capabilities = await async_get_condition_capabilities(mock_hass, config)

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.anyio
    async def test_returns_empty_dict_for_unknown_type(self):
        """Test that empty dict is returned for unknown condition type."""
        mock_hass = MagicMock()
        config = {"type": "unknown_type"}

        capabilities = await async_get_condition_capabilities(mock_hass, config)

        assert capabilities == {}