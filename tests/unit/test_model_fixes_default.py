"""Tests for model_fixes/default.py module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    fix_valve_calibration,
    inital_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)


class TestFixFunctions:
    """Test fix functions that are passthroughs."""

    def test_fix_local_calibration_returns_unchanged(self):
        """Test that fix_local_calibration returns offset unchanged."""
        mock_self = MagicMock()
        result = fix_local_calibration(mock_self, "climate.test_trv", 2.5)
        assert result == 2.5

    def test_fix_valve_calibration_returns_unchanged(self):
        """Test that fix_valve_calibration returns valve unchanged."""
        mock_self = MagicMock()
        result = fix_valve_calibration(mock_self, "climate.test_trv", 50)
        assert result == 50

    def test_fix_target_temperature_calibration_returns_unchanged(self):
        """Test that fix_target_temperature_calibration returns temperature unchanged."""
        mock_self = MagicMock()
        result = fix_target_temperature_calibration(mock_self, "climate.test_trv", 21.5)
        assert result == 21.5


class TestOverrideFunctions:
    """Test override functions."""

    @pytest.mark.anyio
    async def test_override_set_hvac_mode_returns_false(self):
        """Test that override_set_hvac_mode returns False (no override)."""
        mock_self = MagicMock()
        result = await override_set_hvac_mode(mock_self, "climate.test_trv", "heat")
        assert result is False

    @pytest.mark.anyio
    async def test_override_set_temperature_returns_false(self):
        """Test that override_set_temperature returns False (no override)."""
        mock_self = MagicMock()
        result = await override_set_temperature(mock_self, "climate.test_trv", 21.5)
        assert result is False


class TestInitalTweak:
    """Test inital_tweak function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.hass.services.async_call = AsyncMock()
        mock.real_trvs = {
            "climate.test_trv": {
                "advanced": {
                    "child_lock": False,
                }
            }
        }
        return mock

    @pytest.fixture
    def mock_entity_registry(self):
        """Create a mock entity registry."""
        mock_registry = MagicMock()
        mock_entry = MagicMock()
        mock_entry.device_id = "device_123"
        mock_entry.entity_id = "climate.test_trv"
        mock_registry.async_get.return_value = mock_entry

        # Create mock entities for the device
        calibration_entity = MagicMock()
        calibration_entity.device_id = "device_123"
        calibration_entity.domain = "number"
        calibration_entity.entity_id = "number.test_calibration"
        calibration_entity.original_name = "Local Temperature Calibration"
        calibration_entity.unique_id = "test_calibration_uid"

        mock_registry.entities.values.return_value = [calibration_entity]
        return mock_registry

    @pytest.mark.anyio
    async def test_resets_local_calibration_to_zero(
        self, mock_self, mock_entity_registry
    ):
        """Test that local calibration is reset to 0."""
        with patch(
            "custom_components.better_thermostat.model_fixes.default.er.async_get",
            return_value=mock_entity_registry,
        ):
            await inital_tweak(mock_self, "climate.test_trv")

        # Should call number.set_value with value 0
        calls = mock_self.hass.services.async_call.call_args_list
        assert any(
            call[0][0] == "number" and call[0][1] == "set_value" and call[1]["entity_id"] == "number.test_calibration" and call[1]["value"] == 0
            for call in calls
        )

    @pytest.mark.anyio
    async def test_sets_child_lock_when_configured(self, mock_self, mock_entity_registry):
        """Test that child lock is set based on configuration."""
        mock_self.real_trvs["climate.test_trv"]["advanced"]["child_lock"] = True

        # Add child lock entity
        child_lock_entity = MagicMock()
        child_lock_entity.device_id = "device_123"
        child_lock_entity.domain = "switch"
        child_lock_entity.entity_id = "switch.test_child_lock"
        child_lock_entity.original_name = "Child Lock"
        child_lock_entity.unique_id = "test_child_lock_uid"

        mock_entity_registry.entities.values.return_value.append(child_lock_entity)

        mock_state = MagicMock()
        mock_state.state = "off"
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.model_fixes.default.er.async_get",
            return_value=mock_entity_registry,
        ):
            await inital_tweak(mock_self, "climate.test_trv")

        # Should turn on child lock switch
        calls = mock_self.hass.services.async_call.call_args_list
        assert any(
            call[0][0] == "switch" and call[0][1] == "turn_on" and call[1]["entity_id"] == "switch.test_child_lock"
            for call in calls
        )

    @pytest.mark.anyio
    async def test_disables_window_detection(self, mock_self, mock_entity_registry):
        """Test that window detection is disabled."""
        # Add window detection entity
        window_entity = MagicMock()
        window_entity.device_id = "device_123"
        window_entity.domain = "switch"
        window_entity.entity_id = "switch.test_window_detection"
        window_entity.original_name = "Window Detection"
        window_entity.unique_id = "test_window_uid"

        mock_entity_registry.entities.values.return_value.append(window_entity)

        mock_state = MagicMock()
        mock_state.state = "on"
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.model_fixes.default.er.async_get",
            return_value=mock_entity_registry,
        ):
            await inital_tweak(mock_self, "climate.test_trv")

        # Should turn off window detection
        calls = mock_self.hass.services.async_call.call_args_list
        assert any(
            call[0][0] == "switch" and call[0][1] == "turn_off" and call[1]["entity_id"] == "switch.test_window_detection"
            for call in calls
        )

    @pytest.mark.anyio
    async def test_handles_exception_gracefully(self, mock_self, mock_entity_registry):
        """Test that exceptions are handled gracefully."""
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=Exception("Service failed")
        )

        with patch(
            "custom_components.better_thermostat.model_fixes.default.er.async_get",
            return_value=mock_entity_registry,
        ):
            # Should not raise exception
            await inital_tweak(mock_self, "climate.test_trv")

    @pytest.mark.anyio
    async def test_returns_when_entity_not_in_registry(self, mock_self):
        """Test that function returns when entity is not in registry."""
        mock_registry = MagicMock()
        mock_registry.async_get.return_value = None

        with patch(
            "custom_components.better_thermostat.model_fixes.default.er.async_get",
            return_value=mock_registry,
        ):
            await inital_tweak(mock_self, "climate.test_trv")

        # Should not call any services
        mock_self.hass.services.async_call.assert_not_called()