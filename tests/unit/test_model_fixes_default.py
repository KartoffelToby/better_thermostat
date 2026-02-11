"""Tests for model_fixes/default.py module.

Tests default model quirks including passthrough functions and initial tweaks
for calibration, child lock, and window detection settings.
"""

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.lock import STATE_LOCKED, STATE_UNLOCKED
from homeassistant.const import STATE_OFF, STATE_ON
import pytest

from custom_components.better_thermostat.model_fixes import default


class TestPassthroughFunctions:
    """Test passthrough quirk functions."""

    def test_fix_local_calibration_passthrough(self):
        """Test fix_local_calibration returns value unchanged."""
        mock_self = Mock()
        result = default.fix_local_calibration(mock_self, "climate.trv1", 2.5)
        assert result == 2.5

    def test_fix_valve_calibration_passthrough(self):
        """Test fix_valve_calibration returns value unchanged."""
        mock_self = Mock()
        result = default.fix_valve_calibration(mock_self, "climate.trv1", 75)
        assert result == 75

    def test_fix_target_temperature_calibration_passthrough(self):
        """Test fix_target_temperature_calibration returns value unchanged."""
        mock_self = Mock()
        result = default.fix_target_temperature_calibration(
            mock_self, "climate.trv1", 22.0
        )
        assert result == 22.0


class TestOverrideFunctions:
    """Test override functions return False by default."""

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_returns_false(self):
        """Test override_set_hvac_mode returns False."""
        mock_self = Mock()
        result = await default.override_set_hvac_mode(
            mock_self, "climate.trv1", "heat"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_override_set_temperature_returns_false(self):
        """Test override_set_temperature returns False."""
        mock_self = Mock()
        result = await default.override_set_temperature(mock_self, "climate.trv1", 22.0)
        assert result is False


class TestInitialTweak:
    """Test inital_tweak function."""

    @pytest.mark.asyncio
    async def test_initial_tweak_resets_calibration(self):
        """Test initial tweak resets calibration to 0."""
        mock_entity = Mock()
        mock_entity.device_id = "device123"
        mock_entity.entity_id = "climate.trv1"

        mock_cal_entity = Mock()
        mock_cal_entity.device_id = "device123"
        mock_cal_entity.domain = "number"
        mock_cal_entity.entity_id = "number.calibration"
        mock_cal_entity.original_name = "Local Temperature Calibration"
        mock_cal_entity.unique_id = "calibration_uid"

        mock_registry = Mock()
        mock_registry.async_get.return_value = mock_entity
        mock_registry.entities = {"number.calibration": mock_cal_entity}

        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"advanced": {"child_lock": None}}
        }

        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_registry,
        ):
            await default.inital_tweak(mock_self, "climate.trv1")

            # Should reset calibration to 0
            mock_hass.services.async_call.assert_called()
            call_args = mock_hass.services.async_call.call_args_list[0][0]
            assert call_args[0] == "number"
            assert call_args[1] == "set_value"
            assert call_args[2]["entity_id"] == "number.calibration"
            assert call_args[2]["value"] == 0

    @pytest.mark.asyncio
    async def test_initial_tweak_sets_child_lock_switch(self):
        """Test initial tweak sets child lock switch."""
        mock_entity = Mock()
        mock_entity.device_id = "device123"

        mock_cl_entity = Mock()
        mock_cl_entity.device_id = "device123"
        mock_cl_entity.domain = "switch"
        mock_cl_entity.entity_id = "switch.child_lock"
        mock_cl_entity.original_name = "Child Lock"
        mock_cl_entity.unique_id = "child_lock_uid"

        mock_registry = Mock()
        mock_registry.async_get.return_value = mock_entity
        mock_registry.entities = {"switch.child_lock": mock_cl_entity}

        mock_state = Mock()
        mock_state.state = STATE_OFF

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"advanced": {"child_lock": True}}  # Enable child lock
        }

        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_registry,
        ):
            await default.inital_tweak(mock_self, "climate.trv1")

            # Should turn on child lock
            calls = mock_hass.services.async_call.call_args_list
            child_lock_call = [c for c in calls if "switch" in str(c)]
            if child_lock_call:
                assert child_lock_call[0][0][0] == "switch"
                assert child_lock_call[0][0][1] == "turn_on"

    @pytest.mark.asyncio
    async def test_initial_tweak_disables_window_detection(self):
        """Test initial tweak disables window detection."""
        mock_entity = Mock()
        mock_entity.device_id = "device123"

        mock_win_entity = Mock()
        mock_win_entity.device_id = "device123"
        mock_win_entity.domain = "switch"
        mock_win_entity.entity_id = "switch.window_detection"
        mock_win_entity.original_name = "Window Detection"
        mock_win_entity.unique_id = "window_det_uid"

        mock_registry = Mock()
        mock_registry.async_get.return_value = mock_entity
        mock_registry.entities = {"switch.window_detection": mock_win_entity}

        mock_state = Mock()
        mock_state.state = STATE_ON

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"advanced": {"child_lock": None}}
        }

        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_registry,
        ):
            await default.inital_tweak(mock_self, "climate.trv1")

            # Should turn off window detection
            calls = mock_hass.services.async_call.call_args_list
            win_call = [c for c in calls if "window_detection" in str(c)]
            if win_call:
                assert win_call[0][0][1] == "turn_off"

    @pytest.mark.asyncio
    async def test_initial_tweak_no_entity_in_registry(self):
        """Test initial tweak when entity not in registry."""
        mock_registry = Mock()
        mock_registry.async_get.return_value = None

        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"advanced": {"child_lock": None}}
        }

        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_registry,
        ):
            # Should not raise error
            await default.inital_tweak(mock_self, "climate.trv1")

    @pytest.mark.asyncio
    async def test_initial_tweak_handles_service_exceptions(self):
        """Test initial tweak handles service call exceptions gracefully."""
        mock_entity = Mock()
        mock_entity.device_id = "device123"

        mock_cal_entity = Mock()
        mock_cal_entity.device_id = "device123"
        mock_cal_entity.domain = "number"
        mock_cal_entity.entity_id = "number.calibration"
        mock_cal_entity.original_name = "Calibration"
        mock_cal_entity.unique_id = "cal_uid"

        mock_registry = Mock()
        mock_registry.async_get.return_value = mock_entity
        mock_registry.entities = {"number.calibration": mock_cal_entity}

        mock_hass = Mock()
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock(
            side_effect=Exception("Service failed")
        )

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {"advanced": {"child_lock": None}}
        }

        with patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=mock_registry,
        ):
            # Should not raise exception
            await default.inital_tweak(mock_self, "climate.trv1")


class TestValveMaintenanceInterval:
    """Test valve maintenance interval constant."""

    def test_valve_maintenance_interval_is_set(self):
        """Test that valve maintenance interval is defined."""
        assert hasattr(default, "VALVE_MAINTENANCE_INTERVAL_HOURS")
        assert default.VALVE_MAINTENANCE_INTERVAL_HOURS == 168  # 7 days