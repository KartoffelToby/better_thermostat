"""Comprehensive tests for Better Thermostat device automation.

Tests covering device conditions and device triggers for automation support.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat import device_condition, device_trigger


class TestDeviceCondition:
    """Test device condition helpers."""

    @pytest.mark.asyncio
    async def test_async_get_conditions_returns_condition_types(self):
        """Test async_get_conditions returns expected condition types."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        mock_entry = MagicMock()
        mock_entry.domain = "better_thermostat"
        mock_entry.entity_id = "climate.test"

        mock_registry.async_entries_for_device.return_value = [mock_entry]

        with pytest.mock.patch.object(
            er, "async_get", return_value=mock_registry
        ):
            conditions = await device_condition.async_get_conditions(
                mock_hass, "device123"
            )

            assert len(conditions) == 2
            # Should include both hvac_mode and hvac_action conditions
            types = [c["type"] for c in conditions]
            assert "is_hvac_mode" in types
            assert "is_hvac_action" in types

    @pytest.mark.asyncio
    async def test_async_get_conditions_filters_domain(self):
        """Test async_get_conditions filters non-better_thermostat entities."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        # Mix of different domains
        mock_entry1 = MagicMock()
        mock_entry1.domain = "better_thermostat"
        mock_entry1.entity_id = "climate.test1"

        mock_entry2 = MagicMock()
        mock_entry2.domain = "other_domain"
        mock_entry2.entity_id = "climate.test2"

        mock_registry.async_entries_for_device.return_value = [
            mock_entry1,
            mock_entry2,
        ]

        with pytest.mock.patch.object(
            er, "async_get", return_value=mock_registry
        ):
            conditions = await device_condition.async_get_conditions(
                mock_hass, "device123"
            )

            # Should only return conditions for better_thermostat domain
            assert len(conditions) == 2
            assert all(c["domain"] == "better_thermostat" for c in conditions)

    def test_async_condition_from_config_hvac_mode(self):
        """Test async_condition_from_config for HVAC mode condition."""
        mock_hass = MagicMock()
        mock_state = MagicMock()
        mock_state.attributes = {"hvac_mode": HVACMode.HEAT}
        mock_hass.states.get.return_value = mock_state

        config = {
            "type": "is_hvac_mode",
            "entity_id": "climate.test",
            "hvac_mode": HVACMode.HEAT,
        }

        checker = device_condition.async_condition_from_config(mock_hass, config)
        result = checker(mock_hass, None)

        assert result is True

    def test_async_condition_from_config_hvac_mode_mismatch(self):
        """Test async_condition_from_config returns False on mode mismatch."""
        mock_hass = MagicMock()
        mock_state = MagicMock()
        mock_state.attributes = {"hvac_mode": HVACMode.OFF}
        mock_hass.states.get.return_value = mock_state

        config = {
            "type": "is_hvac_mode",
            "entity_id": "climate.test",
            "hvac_mode": HVACMode.HEAT,
        }

        checker = device_condition.async_condition_from_config(mock_hass, config)
        result = checker(mock_hass, None)

        assert result is False

    def test_async_condition_from_config_hvac_action(self):
        """Test async_condition_from_config for HVAC action condition."""
        mock_hass = MagicMock()
        mock_state = MagicMock()
        mock_state.attributes = {"hvac_action": HVACAction.HEATING}
        mock_hass.states.get.return_value = mock_state

        config = {
            "type": "is_hvac_action",
            "entity_id": "climate.test",
            "hvac_action": HVACAction.HEATING,
        }

        checker = device_condition.async_condition_from_config(mock_hass, config)
        result = checker(mock_hass, None)

        assert result is True

    def test_async_condition_from_config_none_state(self):
        """Test async_condition_from_config handles None state."""
        mock_hass = MagicMock()
        mock_hass.states.get.return_value = None

        config = {
            "type": "is_hvac_mode",
            "entity_id": "climate.test",
            "hvac_mode": HVACMode.HEAT,
        }

        checker = device_condition.async_condition_from_config(mock_hass, config)
        result = checker(mock_hass, None)

        assert result is False

    def test_async_condition_from_config_unknown_type_returns_false(self):
        """Test async_condition_from_config returns false function for unknown type."""
        mock_hass = MagicMock()

        config = {"type": "unknown_type", "entity_id": "climate.test"}

        checker = device_condition.async_condition_from_config(mock_hass, config)
        result = checker(mock_hass, None)

        assert result is False

    @pytest.mark.asyncio
    async def test_async_get_condition_capabilities_hvac_mode(self):
        """Test async_get_condition_capabilities for hvac_mode."""
        mock_hass = MagicMock()
        config = {"type": "is_hvac_mode"}

        capabilities = await device_condition.async_get_condition_capabilities(
            mock_hass, config
        )

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.asyncio
    async def test_async_get_condition_capabilities_hvac_action(self):
        """Test async_get_condition_capabilities for hvac_action."""
        mock_hass = MagicMock()
        config = {"type": "is_hvac_action"}

        capabilities = await device_condition.async_get_condition_capabilities(
            mock_hass, config
        )

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None


class TestDeviceTrigger:
    """Test device trigger helpers."""

    @pytest.mark.asyncio
    async def test_async_get_triggers_returns_trigger_types(self):
        """Test async_get_triggers returns expected trigger types."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        mock_entry = MagicMock()
        mock_entry.domain = "better_thermostat"
        mock_entry.entity_id = "climate.test"

        mock_registry.async_entries_for_device.return_value = [mock_entry]

        mock_state = MagicMock()
        mock_hass.states.get.return_value = mock_state

        with pytest.mock.patch.object(
            er, "async_get", return_value=mock_registry
        ):
            triggers = await device_trigger.async_get_triggers(mock_hass, "device123")

            # Should return multiple trigger types
            assert len(triggers) >= 3
            types = [t["type"] for t in triggers]
            assert "hvac_mode_changed" in types
            assert "current_temperature_changed" in types
            assert "current_humidity_changed" in types

    @pytest.mark.asyncio
    async def test_async_get_triggers_filters_no_state(self):
        """Test async_get_triggers filters entities with no state."""
        mock_hass = MagicMock()
        mock_registry = MagicMock()

        mock_entry = MagicMock()
        mock_entry.domain = "better_thermostat"
        mock_entry.entity_id = "climate.test"

        mock_registry.async_entries_for_device.return_value = [mock_entry]
        mock_hass.states.get.return_value = None  # No state

        with pytest.mock.patch.object(
            er, "async_get", return_value=mock_registry
        ):
            triggers = await device_trigger.async_get_triggers(mock_hass, "device123")

            # Should return empty list when no state
            assert len(triggers) == 0

    @pytest.mark.asyncio
    async def test_async_attach_trigger_hvac_mode_changed(self):
        """Test async_attach_trigger for hvac_mode_changed."""
        mock_hass = MagicMock()
        mock_action = AsyncMock()
        mock_trigger_info = MagicMock()

        config = {
            "type": "hvac_mode_changed",
            "entity_id": "climate.test",
            "to": HVACMode.HEAT,
        }

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_trigger.state_trigger"
        ) as mock_state_trigger:
            mock_state_trigger.async_validate_trigger_config = AsyncMock(
                return_value=config
            )
            mock_state_trigger.async_attach_trigger = AsyncMock()

            await device_trigger.async_attach_trigger(
                mock_hass, config, mock_action, mock_trigger_info
            )

            # Should validate and attach state trigger
            mock_state_trigger.async_validate_trigger_config.assert_called_once()
            mock_state_trigger.async_attach_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_attach_trigger_temperature_changed(self):
        """Test async_attach_trigger for current_temperature_changed."""
        mock_hass = MagicMock()
        mock_action = AsyncMock()
        mock_trigger_info = MagicMock()

        config = {
            "type": "current_temperature_changed",
            "entity_id": "climate.test",
            "above": 22.0,
        }

        with pytest.mock.patch(
            "custom_components.better_thermostat.device_trigger.numeric_state_trigger"
        ) as mock_numeric:
            mock_numeric.async_validate_trigger_config = AsyncMock(return_value=config)
            mock_numeric.async_attach_trigger = AsyncMock()

            await device_trigger.async_attach_trigger(
                mock_hass, config, mock_action, mock_trigger_info
            )

            # Should validate and attach numeric state trigger
            mock_numeric.async_validate_trigger_config.assert_called_once()
            mock_numeric.async_attach_trigger.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_get_trigger_capabilities_hvac_mode(self):
        """Test async_get_trigger_capabilities for hvac_mode_changed."""
        mock_hass = MagicMock()
        config = {"type": "hvac_mode_changed"}

        capabilities = await device_trigger.async_get_trigger_capabilities(
            mock_hass, config
        )

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.asyncio
    async def test_async_get_trigger_capabilities_temperature(self):
        """Test async_get_trigger_capabilities for temperature trigger."""
        mock_hass = MagicMock()
        mock_hass.config.units.temperature_unit = "°C"

        config = {"type": "current_temperature_changed"}

        capabilities = await device_trigger.async_get_trigger_capabilities(
            mock_hass, config
        )

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.asyncio
    async def test_async_get_trigger_capabilities_humidity(self):
        """Test async_get_trigger_capabilities for humidity trigger."""
        mock_hass = MagicMock()

        config = {"type": "current_humidity_changed"}

        capabilities = await device_trigger.async_get_trigger_capabilities(
            mock_hass, config
        )

        assert "extra_fields" in capabilities
        assert capabilities["extra_fields"] is not None

    @pytest.mark.asyncio
    async def test_async_get_trigger_capabilities_unknown_type(self):
        """Test async_get_trigger_capabilities returns empty for unknown type."""
        mock_hass = MagicMock()

        config = {"type": "unknown_trigger_type"}

        capabilities = await device_trigger.async_get_trigger_capabilities(
            mock_hass, config
        )

        assert capabilities == {}