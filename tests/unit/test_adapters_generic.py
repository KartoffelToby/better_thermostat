"""Tests for adapters/generic.py module.

Tests the generic adapter functions including offset retrieval,
calibration entity handling, and set operations.
"""

from unittest.mock import AsyncMock, Mock, patch

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from custom_components.better_thermostat.adapters import generic


class TestGetCurrentOffset:
    """Test get_current_offset function."""

    @pytest.mark.asyncio
    async def test_get_current_offset_with_valid_state(self):
        """Test getting current offset with valid state."""
        mock_state = Mock()
        mock_state.state = "2.5"

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration"
            }
        }

        result = await generic.get_current_offset(mock_self, "climate.trv1")
        assert result == 2.5

    @pytest.mark.asyncio
    async def test_get_current_offset_with_k_suffix(self):
        """Test getting offset from SELECT entity with 'k' suffix."""
        mock_state = Mock()
        mock_state.state = "1.5k"

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "select.calibration"
            }
        }

        result = await generic.get_current_offset(mock_self, "climate.trv1")
        assert result == 1.5

    @pytest.mark.asyncio
    async def test_get_current_offset_unavailable(self):
        """Test getting offset when state is unavailable."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration"
            }
        }

        result = await generic.get_current_offset(mock_self, "climate.trv1")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_current_offset_no_entity(self):
        """Test getting offset when no calibration entity is configured."""
        mock_self = Mock()
        mock_self.real_trvs = {
            "climate.trv1": {"local_temperature_calibration_entity": None}
        }

        result = await generic.get_current_offset(mock_self, "climate.trv1")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_current_offset_invalid_value(self):
        """Test getting offset when state value is invalid."""
        mock_state = Mock()
        mock_state.state = "invalid"

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration"
            }
        }

        result = await generic.get_current_offset(mock_self, "climate.trv1")
        assert result == 0.0


class TestGetMinMaxOffset:
    """Test get_min_offset and get_max_offset functions."""

    @pytest.mark.asyncio
    async def test_get_min_offset_from_number_entity(self):
        """Test getting min offset from NUMBER entity."""
        mock_state = Mock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration"
            }
        }

        result = await generic.get_min_offset(mock_self, "climate.trv1")
        assert result == -5.0

    @pytest.mark.asyncio
    async def test_get_max_offset_from_number_entity(self):
        """Test getting max offset from NUMBER entity."""
        mock_state = Mock()
        mock_state.domain = "number"
        mock_state.attributes = {"max": 5.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration"
            }
        }

        result = await generic.get_max_offset(mock_self, "climate.trv1")
        assert result == 5.0

    @pytest.mark.asyncio
    async def test_get_min_offset_from_select_entity(self):
        """Test getting min offset from SELECT entity options."""
        mock_state = Mock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-3.0k", "-2.0k", "0.0k", "2.0k", "3.0k"]}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "select.calibration"
            }
        }

        result = await generic.get_min_offset(mock_self, "climate.trv1")
        assert result == -3.0

    @pytest.mark.asyncio
    async def test_get_max_offset_from_select_entity(self):
        """Test getting max offset from SELECT entity options."""
        mock_state = Mock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-3.0k", "-2.0k", "0.0k", "2.0k", "3.0k"]}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "select.calibration"
            }
        }

        result = await generic.get_max_offset(mock_self, "climate.trv1")
        assert result == 3.0

    @pytest.mark.asyncio
    async def test_get_min_offset_no_entity(self):
        """Test getting min offset when no entity configured."""
        mock_self = Mock()
        mock_self.real_trvs = {
            "climate.trv1": {"local_temperature_calibration_entity": None}
        }

        result = await generic.get_min_offset(mock_self, "climate.trv1")
        assert result == -6

    @pytest.mark.asyncio
    async def test_get_max_offset_no_entity(self):
        """Test getting max offset when no entity configured."""
        mock_self = Mock()
        mock_self.real_trvs = {
            "climate.trv1": {"local_temperature_calibration_entity": None}
        }

        result = await generic.get_max_offset(mock_self, "climate.trv1")
        assert result == 6


class TestSetOffset:
    """Test set_offset function."""

    @pytest.mark.asyncio
    async def test_set_offset_number_entity(self):
        """Test setting offset for NUMBER entity."""
        mock_state = Mock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0, "max": 5.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration",
                "last_calibration": 0.0,
                "last_hvac_mode": None,
            }
        }

        await generic.set_offset(mock_self, "climate.trv1", 2.5)

        # Should call number.set_value service
        mock_hass.services.async_call.assert_called()
        call_args = mock_hass.services.async_call.call_args[0]
        assert call_args[0] == "number"
        assert call_args[1] == "set_value"
        assert call_args[2]["entity_id"] == "number.calibration"
        assert call_args[2]["value"] == 2.5

    @pytest.mark.asyncio
    async def test_set_offset_select_entity(self):
        """Test setting offset for SELECT entity."""
        mock_state = Mock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-3.0k", "0.0k", "1.5k", "3.0k"]}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "select.calibration",
                "last_calibration": 0.0,
                "last_hvac_mode": None,
            }
        }

        await generic.set_offset(mock_self, "climate.trv1", 1.5)

        # Should call select.select_option service
        call_args = mock_hass.services.async_call.call_args[0]
        assert call_args[0] == "select"
        assert call_args[1] == "select_option"
        assert call_args[2]["entity_id"] == "select.calibration"
        assert call_args[2]["option"] == "1.5k"

    @pytest.mark.asyncio
    async def test_set_offset_select_snap_to_closest(self):
        """Test setting offset snaps to closest option for SELECT entity."""
        mock_state = Mock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-3.0k", "0.0k", "1.0k", "3.0k"]}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "select.calibration",
                "last_calibration": 0.0,
                "last_hvac_mode": None,
            }
        }

        # 1.4 should snap to 1.0k (closest option)
        await generic.set_offset(mock_self, "climate.trv1", 1.4)

        call_args = mock_hass.services.async_call.call_args[0]
        assert call_args[2]["option"] == "1.0k"

    @pytest.mark.asyncio
    async def test_set_offset_clamping_to_max(self):
        """Test offset is clamped to maximum."""
        mock_state = Mock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0, "max": 5.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration",
                "last_calibration": 0.0,
                "last_hvac_mode": None,
            }
        }

        # 7.0 should clamp to 5.0
        await generic.set_offset(mock_self, "climate.trv1", 7.0)

        call_args = mock_hass.services.async_call.call_args[0]
        assert call_args[2]["value"] == 5.0

    @pytest.mark.asyncio
    async def test_set_offset_clamping_to_min(self):
        """Test offset is clamped to minimum."""
        mock_state = Mock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0, "max": 5.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.device_name = "test_device"
        mock_self.hass = mock_hass
        mock_self.context = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "local_temperature_calibration_entity": "number.calibration",
                "last_calibration": 0.0,
                "last_hvac_mode": None,
            }
        }

        # -7.0 should clamp to -5.0
        await generic.set_offset(mock_self, "climate.trv1", -7.0)

        call_args = mock_hass.services.async_call.call_args[0]
        assert call_args[2]["value"] == -5.0

    @pytest.mark.asyncio
    async def test_set_offset_no_entity_returns_none(self):
        """Test that no offset is set when entity is None."""
        mock_self = Mock()
        mock_self.real_trvs = {
            "climate.trv1": {"local_temperature_calibration_entity": None}
        }

        result = await generic.set_offset(mock_self, "climate.trv1", 2.5)
        assert result is None


class TestGetInfo:
    """Test get_info function."""

    @pytest.mark.asyncio
    async def test_get_info_with_offset_support(self):
        """Test get_info when offset is supported."""
        mock_self = Mock()

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
            return_value="number.calibration",
        ):
            result = await generic.get_info(mock_self, "climate.trv1")

            assert result["support_offset"] is True
            assert result["support_valve"] is False

    @pytest.mark.asyncio
    async def test_get_info_without_offset_support(self):
        """Test get_info when offset is not supported."""
        mock_self = Mock()

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await generic.get_info(mock_self, "climate.trv1")

            assert result["support_offset"] is False
            assert result["support_valve"] is False