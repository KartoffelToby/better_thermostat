"""Tests for adapters/generic.py module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.adapters.generic import (
    get_current_offset,
    get_info,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    set_hvac_mode,
    set_offset,
    set_temperature,
    set_valve,
)


class TestGetInfo:
    """Test get_info function."""

    @pytest.mark.anyio
    async def test_returns_support_offset_true_when_calibration_entity_found(self):
        """Test that support_offset is True when calibration entity is found."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
            return_value="number.test_calibration",
        ):
            result = await get_info(mock_self, "climate.test_trv")

        assert result["support_offset"] is True
        assert result["support_valve"] is False

    @pytest.mark.anyio
    async def test_returns_support_offset_false_when_no_calibration_entity(self):
        """Test that support_offset is False when no calibration entity found."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_info(mock_self, "climate.test_trv")

        assert result["support_offset"] is False
        assert result["support_valve"] is False


class TestInit:
    """Test init function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.real_trvs = {
            "climate.test_trv": {
                "local_temperature_calibration_entity": None,
                "calibration": 0,
            }
        }
        return mock

    @pytest.mark.anyio
    async def test_finds_and_registers_calibration_entity(self, mock_self):
        """Test that init finds and registers calibration entity."""
        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
            return_value="number.test_calibration",
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.wait_for_calibration_entity_or_timeout",
                new_callable=AsyncMock,
            ):
                await init(mock_self, "climate.test_trv")

        assert (
            mock_self.real_trvs["climate.test_trv"][
                "local_temperature_calibration_entity"
            ]
            == "number.test_calibration"
        )

    @pytest.mark.anyio
    async def test_waits_for_calibration_entity_when_found(self, mock_self):
        """Test that init waits for calibration entity to be available."""
        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
            return_value="number.test_calibration",
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.wait_for_calibration_entity_or_timeout",
                new_callable=AsyncMock,
            ) as mock_wait:
                await init(mock_self, "climate.test_trv")

        mock_wait.assert_called_once()

    @pytest.mark.anyio
    async def test_skips_init_when_calibration_already_set(self, mock_self):
        """Test that init is skipped when calibration entity is already set."""
        mock_self.real_trvs["climate.test_trv"][
            "local_temperature_calibration_entity"
        ] = "number.existing"

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
        ) as mock_find:
            await init(mock_self, "climate.test_trv")

        # Should not attempt to find calibration entity
        mock_find.assert_not_called()

    @pytest.mark.anyio
    async def test_skips_init_when_calibration_disabled(self, mock_self):
        """Test that init is skipped when calibration is disabled."""
        mock_self.real_trvs["climate.test_trv"]["calibration"] = 1

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity",
            new_callable=AsyncMock,
        ) as mock_find:
            await init(mock_self, "climate.test_trv")

        mock_find.assert_not_called()


class TestGetCurrentOffset:
    """Test get_current_offset function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.real_trvs = {
            "climate.test_trv": {
                "local_temperature_calibration_entity": "number.test_calibration",
            }
        }
        return mock

    @pytest.mark.anyio
    async def test_returns_offset_from_number_entity(self, mock_self):
        """Test that function returns offset from number entity state."""
        mock_state = MagicMock()
        mock_state.state = "1.5"
        mock_self.hass.states.get.return_value = mock_state

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 1.5

    @pytest.mark.anyio
    async def test_returns_zero_when_state_unavailable(self, mock_self):
        """Test that function returns 0.0 when state is unavailable."""
        mock_state = MagicMock()
        mock_state.state = "unavailable"
        mock_self.hass.states.get.return_value = mock_state

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 0.0

    @pytest.mark.anyio
    async def test_returns_zero_when_state_is_none(self, mock_self):
        """Test that function returns 0.0 when state is None."""
        mock_self.hass.states.get.return_value = None

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 0.0

    @pytest.mark.anyio
    async def test_removes_k_suffix_from_select_entity(self, mock_self):
        """Test that 'k' suffix is removed from SELECT entity state."""
        mock_state = MagicMock()
        mock_state.state = "2.5k"
        mock_self.hass.states.get.return_value = mock_state

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 2.5

    @pytest.mark.anyio
    async def test_returns_zero_when_conversion_fails(self, mock_self):
        """Test that function returns 0.0 when conversion fails."""
        mock_state = MagicMock()
        mock_state.state = "invalid"
        mock_self.hass.states.get.return_value = mock_state

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 0.0

    @pytest.mark.anyio
    async def test_returns_zero_when_no_calibration_entity(self, mock_self):
        """Test that function returns 0.0 when no calibration entity is set."""
        mock_self.real_trvs["climate.test_trv"][
            "local_temperature_calibration_entity"
        ] = None

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 0.0


class TestGetOffsetBounds:
    """Test get_min_offset and get_max_offset functions."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.real_trvs = {
            "climate.test_trv": {
                "local_temperature_calibration_entity": "number.test_calibration",
            }
        }
        return mock

    @pytest.mark.anyio
    async def test_get_min_offset_returns_min_attribute(self, mock_self):
        """Test that get_min_offset returns min attribute from state."""
        mock_state = MagicMock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -10.0}
        mock_self.hass.states.get.return_value = mock_state

        result = await get_min_offset(mock_self, "climate.test_trv")

        assert result == -10.0

    @pytest.mark.anyio
    async def test_get_max_offset_returns_max_attribute(self, mock_self):
        """Test that get_max_offset returns max attribute from state."""
        mock_state = MagicMock()
        mock_state.domain = "number"
        mock_state.attributes = {"max": 10.0}
        mock_self.hass.states.get.return_value = mock_state

        result = await get_max_offset(mock_self, "climate.test_trv")

        assert result == 10.0

    @pytest.mark.anyio
    async def test_get_min_offset_from_select_options(self, mock_self):
        """Test that get_min_offset extracts min from SELECT options."""
        mock_state = MagicMock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-5.0k", "-2.5k", "0.0k", "2.5k", "5.0k"]}
        mock_self.hass.states.get.return_value = mock_state

        result = await get_min_offset(mock_self, "climate.test_trv")

        assert result == -5.0

    @pytest.mark.anyio
    async def test_get_max_offset_from_select_options(self, mock_self):
        """Test that get_max_offset extracts max from SELECT options."""
        mock_state = MagicMock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-5.0k", "-2.5k", "0.0k", "2.5k", "5.0k"]}
        mock_self.hass.states.get.return_value = mock_state

        result = await get_max_offset(mock_self, "climate.test_trv")

        assert result == 5.0

    @pytest.mark.anyio
    async def test_get_min_offset_default_when_none(self, mock_self):
        """Test that get_min_offset returns default when state is None."""
        mock_self.hass.states.get.return_value = None

        result = await get_min_offset(mock_self, "climate.test_trv")

        assert result == -6.0

    @pytest.mark.anyio
    async def test_get_max_offset_default_when_none(self, mock_self):
        """Test that get_max_offset returns default when state is None."""
        mock_self.hass.states.get.return_value = None

        result = await get_max_offset(mock_self, "climate.test_trv")

        assert result == 6.0


class TestSetOffset:
    """Test set_offset function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.context = MagicMock()
        mock.real_trvs = {
            "climate.test_trv": {
                "local_temperature_calibration_entity": "number.test_calibration",
                "last_hvac_mode": "heat",
            }
        }
        mock.hass.services.async_call = AsyncMock()
        return mock

    @pytest.mark.anyio
    async def test_clamps_offset_to_min_max(self, mock_self):
        """Test that offset is clamped to min/max values."""
        mock_state = MagicMock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0, "max": 5.0}
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.adapters.generic.get_max_offset",
            return_value=5.0,
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.get_min_offset",
                return_value=-5.0,
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    result = await set_offset(mock_self, "climate.test_trv", 10.0)

        # Should be clamped to max (5.0)
        assert result == 5.0

    @pytest.mark.anyio
    async def test_uses_number_service_for_number_entity(self, mock_self):
        """Test that number.set_value service is used for NUMBER entities."""
        mock_state = MagicMock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0, "max": 5.0}
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.adapters.generic.get_max_offset",
            return_value=5.0,
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.get_min_offset",
                return_value=-5.0,
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await set_offset(mock_self, "climate.test_trv", 2.5)

        # Should call number.set_value
        assert mock_self.hass.services.async_call.called
        call_args = mock_self.hass.services.async_call.call_args_list[0]
        assert call_args[0][0] == "number"
        assert call_args[0][1] == "set_value"

    @pytest.mark.anyio
    async def test_uses_select_service_for_select_entity(self, mock_self):
        """Test that select.select_option service is used for SELECT entities."""
        mock_state = MagicMock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-5.0k", "0.0k", "2.5k", "5.0k"]}
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.adapters.generic.get_max_offset",
            return_value=5.0,
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.get_min_offset",
                return_value=-5.0,
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await set_offset(mock_self, "climate.test_trv", 2.5)

        # Should call select.select_option with "2.5k" format
        assert mock_self.hass.services.async_call.called
        call_args = mock_self.hass.services.async_call.call_args_list[0]
        assert call_args[0][0] == "select"
        assert call_args[0][1] == "select_option"
        assert call_args[1]["service_data"]["option"] == "2.5k"

    @pytest.mark.anyio
    async def test_snaps_to_closest_select_option(self, mock_self):
        """Test that offset snaps to closest available SELECT option."""
        mock_state = MagicMock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["-5.0k", "0.0k", "2.5k", "5.0k"]}
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.adapters.generic.get_max_offset",
            return_value=5.0,
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.get_min_offset",
                return_value=-5.0,
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await set_offset(mock_self, "climate.test_trv", 1.8)

        # Should snap to closest option "2.5k"
        call_args = mock_self.hass.services.async_call.call_args_list[0]
        assert call_args[1]["service_data"]["option"] == "2.5k"

    @pytest.mark.anyio
    async def test_updates_last_calibration(self, mock_self):
        """Test that last_calibration is updated."""
        mock_state = MagicMock()
        mock_state.domain = "number"
        mock_state.attributes = {"min": -5.0, "max": 5.0}
        mock_self.hass.states.get.return_value = mock_state

        with patch(
            "custom_components.better_thermostat.adapters.generic.get_max_offset",
            return_value=5.0,
        ):
            with patch(
                "custom_components.better_thermostat.adapters.generic.get_min_offset",
                return_value=-5.0,
            ):
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await set_offset(mock_self, "climate.test_trv", 2.5)

        assert mock_self.real_trvs["climate.test_trv"]["last_calibration"] == 2.5


class TestSetTemperature:
    """Test set_temperature function."""

    @pytest.mark.anyio
    async def test_calls_climate_service(self):
        """Test that climate.set_temperature service is called."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock()

        await set_temperature(mock_self, "climate.test_trv", 21.5)

        mock_self.hass.services.async_call.assert_called_once_with(
            "climate",
            "set_temperature",
            {"entity_id": "climate.test_trv", "temperature": 21.5},
            blocking=True,
            context=mock_self.context,
        )


class TestSetHvacMode:
    """Test set_hvac_mode function."""

    @pytest.mark.anyio
    async def test_normalizes_and_calls_climate_service(self):
        """Test that HVAC mode is normalized and climate service is called."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock()

        with patch(
            "custom_components.better_thermostat.adapters.generic.normalize_hvac_mode",
            return_value="heat",
        ):
            await set_hvac_mode(mock_self, "climate.test_trv", "heat")

        mock_self.hass.services.async_call.assert_called_once()

    @pytest.mark.anyio
    async def test_handles_type_error_gracefully(self):
        """Test that TypeError is handled gracefully."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock(side_effect=TypeError("Error"))

        with patch(
            "custom_components.better_thermostat.adapters.generic.normalize_hvac_mode",
            return_value="heat",
        ):
            # Should not raise exception
            await set_hvac_mode(mock_self, "climate.test_trv", "heat")


class TestSetValve:
    """Test set_valve function."""

    @pytest.mark.anyio
    async def test_returns_none_for_unsupported_operation(self):
        """Test that set_valve returns None (not supported)."""
        mock_self = MagicMock()

        result = await set_valve(mock_self, "climate.test_trv", 50)

        # Generic adapter does not support valve control
        assert result is None