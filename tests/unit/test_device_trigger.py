"""Tests for device_trigger.py module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.better_thermostat.device_trigger import (
    async_get_trigger_capabilities,
    async_get_triggers,
)


class TestAsyncGetTriggers:
    """Test async_get_triggers function."""

    @pytest.mark.anyio
    async def test_returns_triggers_for_better_thermostat_device(self):
        """Test that triggers are returned for Better Thermostat device."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        # Create mock entity
        mock_entry = MagicMock()
        mock_entry.domain = "better_thermostat"
        mock_entry.entity_id = "climate.test_thermostat"

        mock_state = MagicMock()
        mock_state.attributes = {"temperature": 21.0}
        mock_hass.states.get.return_value = mock_state

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_trigger.entity_registry.async_get",
            return_value=mock_registry,
        ):
            with pytest.mock.patch(
                "custom_components.better_thermostat.device_trigger.entity_registry.async_entries_for_device",
                return_value=[mock_entry],
            ):
                triggers = await async_get_triggers(mock_hass, "device_123")

        # Should return 3 triggers
        assert len(triggers) == 3
        assert any(t["type"] == "hvac_mode_changed" for t in triggers)
        assert any(t["type"] == "current_temperature_changed" for t in triggers)
        assert any(t["type"] == "current_humidity_changed" for t in triggers)

    @pytest.mark.anyio
    async def test_returns_empty_list_when_no_better_thermostat_entities(self):
        """Test that empty list is returned when no better_thermostat entities."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        mock_entry = MagicMock()
        mock_entry.domain = "sensor"
        mock_entry.entity_id = "sensor.test"

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_trigger.entity_registry.async_get",
            return_value=mock_registry,
        ):
            with pytest.mock.patch(
                "custom_components.better_thermostat.device_trigger.entity_registry.async_entries_for_device",
                return_value=[mock_entry],
            ):
                triggers = await async_get_triggers(mock_hass, "device_123")

        assert len(triggers) == 0

    @pytest.mark.anyio
    async def test_skips_entities_without_state(self):
        """Test that entities without state are skipped."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        mock_entry = MagicMock()
        mock_entry.domain = "better_thermostat"
        mock_entry.entity_id = "climate.test_thermostat"

        mock_hass.states.get.return_value = None

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_trigger.entity_registry.async_get",
            return_value=mock_registry,
        ):
            with pytest.mock.patch(
                "custom_components.better_thermostat.device_trigger.entity_registry.async_entries_for_device",
                return_value=[mock_entry],
            ):
                triggers = await async_get_triggers(mock_hass, "device_123")

        assert len(triggers) == 0


class TestAsyncGetTriggerCapabilities:
    """Test async_get_trigger_capabilities function."""

    @pytest.mark.anyio
    async def test_returns_hvac_mode_changed_capabilities(self):
        """Test that HVAC mode changed capabilities are returned."""
        mock_hass = MagicMock()
        config = {"type": "hvac_mode_changed"}

        capabilities = await async_get_trigger_capabilities(mock_hass, config)

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.anyio
    async def test_returns_temperature_changed_capabilities(self):
        """Test that temperature changed capabilities are returned."""
        mock_hass = MagicMock()
        mock_hass.config.units.temperature_unit = "°C"
        config = {"type": "current_temperature_changed"}

        capabilities = await async_get_trigger_capabilities(mock_hass, config)

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.anyio
    async def test_returns_humidity_changed_capabilities(self):
        """Test that humidity changed capabilities are returned."""
        mock_hass = MagicMock()
        config = {"type": "current_humidity_changed"}

        capabilities = await async_get_trigger_capabilities(mock_hass, config)

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.anyio
    async def test_returns_empty_dict_for_unknown_type(self):
        """Test that empty dict is returned for unknown trigger type."""
        mock_hass = MagicMock()
        config = {"type": "unknown_type"}

        capabilities = await async_get_trigger_capabilities(mock_hass, config)

        assert capabilities == {}