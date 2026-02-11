"""Tests for adapters/delegate.py module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.adapters.delegate import (
    get_current_offset,
    get_info,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    load_adapter,
    set_hvac_mode,
    set_offset,
    set_temperature,
    set_valve,
)


class TestLoadAdapter:
    """Test load_adapter function."""

    @pytest.mark.anyio
    async def test_loads_specific_adapter_when_available(self):
        """Test that load_adapter loads a specific adapter when it exists."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()

        mock_adapter = MagicMock()
        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module",
            new_callable=AsyncMock,
            return_value=mock_adapter,
        ) as mock_import:
            result = await load_adapter(
                mock_self, "zigbee2mqtt", "climate.test_trv", get_name=False
            )

        assert result == mock_adapter
        mock_import.assert_called_once_with(
            mock_self.hass, "custom_components.better_thermostat.adapters.zigbee2mqtt"
        )

    @pytest.mark.anyio
    async def test_remaps_generic_thermostat_to_generic(self):
        """Test that 'generic_thermostat' is remapped to 'generic'."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()

        mock_adapter = MagicMock()
        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module",
            new_callable=AsyncMock,
            return_value=mock_adapter,
        ):
            await load_adapter(
                mock_self, "generic_thermostat", "climate.test_trv", get_name=False
            )

        # Should load 'generic' adapter, not 'generic_thermostat'
        assert mock_self.adapter == mock_adapter

    @pytest.mark.anyio
    async def test_falls_back_to_generic_when_adapter_not_found(self):
        """Test that load_adapter falls back to generic adapter when specific adapter fails."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()

        mock_generic_adapter = MagicMock()

        async def import_side_effect(hass, module_name):
            if "unknown_integration" in module_name:
                raise ImportError("Module not found")
            return mock_generic_adapter

        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module",
            side_effect=import_side_effect,
        ):
            result = await load_adapter(
                mock_self, "unknown_integration", "climate.test_trv", get_name=False
            )

        assert result == mock_generic_adapter

    @pytest.mark.anyio
    async def test_returns_integration_name_when_get_name_is_true(self):
        """Test that function returns integration name when get_name=True."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.hass = MagicMock()

        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ):
            result = await load_adapter(
                mock_self, "zigbee2mqtt", "climate.test_trv", get_name=True
            )

        assert result == "zigbee2mqtt"


class TestSetTemperature:
    """Test set_temperature function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.context = MagicMock()
        mock.real_trvs = {
            "climate.test_trv": {
                "adapter": MagicMock(),
                "last_temperature": 20.0,
                "target_temp_step": 0.5,
                "min_temp": 5.0,
                "max_temp": 30.0,
            }
        }
        mock.real_trvs["climate.test_trv"]["adapter"].set_temperature = AsyncMock()
        mock.bt_target_temp_step = None
        return mock

    @pytest.mark.anyio
    async def test_rounds_temperature_to_device_step(self, mock_self):
        """Test that temperature is rounded to device step."""
        await set_temperature(mock_self, "climate.test_trv", 21.3)

        # Should round 21.3 to 21.5 with step 0.5
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_temperature.assert_called_once()
        call_args = mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_temperature.call_args
        assert abs(call_args[0][2] - 21.5) < 0.01

    @pytest.mark.anyio
    async def test_clamps_temperature_to_min_max(self, mock_self):
        """Test that temperature is clamped to min/max values."""
        # Test max clamping
        await set_temperature(mock_self, "climate.test_trv", 35.0)
        call_args = mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_temperature.call_args
        assert call_args[0][2] == 30.0

        # Test min clamping
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_temperature.reset_mock()
        await set_temperature(mock_self, "climate.test_trv", 3.0)
        call_args = mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_temperature.call_args
        assert call_args[0][2] == 5.0

    @pytest.mark.anyio
    async def test_updates_last_temperature(self, mock_self):
        """Test that last_temperature is updated with the rounded value."""
        await set_temperature(mock_self, "climate.test_trv", 21.3)

        # Should update last_temperature to the rounded value
        assert abs(mock_self.real_trvs["climate.test_trv"]["last_temperature"] - 21.5) < 0.01

    @pytest.mark.anyio
    async def test_handles_invalid_temperature_input(self, mock_self):
        """Test that function handles invalid temperature input."""
        await set_temperature(mock_self, "climate.test_trv", "invalid")

        # Should convert to 0.0 and clamp to min_temp
        call_args = mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_temperature.call_args
        assert call_args[0][2] == 5.0

    @pytest.mark.anyio
    async def test_uses_global_config_step_when_per_trv_not_set(self, mock_self):
        """Test that global config step is used when per-TRV step is not set."""
        mock_self.real_trvs["climate.test_trv"]["target_temp_step"] = None
        mock_self.bt_target_temp_step = 1.0

        state = MagicMock()
        state.attributes = {"target_temp_step": None}
        mock_self.hass.states.get.return_value = state

        await set_temperature(mock_self, "climate.test_trv", 21.3)

        # Should round to 21.0 with step 1.0
        call_args = mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_temperature.call_args
        assert call_args[0][2] == 21.0

    @pytest.mark.anyio
    async def test_uses_device_step_from_state_attributes(self, mock_self):
        """Test that device step from state attributes is used."""
        mock_self.real_trvs["climate.test_trv"]["target_temp_step"] = None
        mock_self.bt_target_temp_step = None

        state = MagicMock()
        state.attributes = {"target_temp_step": 0.2}
        mock_self.hass.states.get.return_value = state

        await set_temperature(mock_self, "climate.test_trv", 21.15)

        # Should round to 21.2 with step 0.2
        call_args = mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_temperature.call_args
        assert abs(call_args[0][2] - 21.2) < 0.01


class TestSetValve:
    """Test set_valve function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.hass = MagicMock()
        mock.real_trvs = {
            "climate.test_trv": {
                "adapter": MagicMock(),
                "valve_position_entity": "number.test_valve",
                "valve_position_writable": True,
                "model_quirks": MagicMock(),
            }
        }
        mock.real_trvs["climate.test_trv"]["adapter"].set_valve = AsyncMock()
        return mock

    @pytest.mark.anyio
    async def test_uses_override_set_valve_when_available(self, mock_self):
        """Test that override_set_valve from model_quirks is used when available."""
        override_func = AsyncMock(return_value=True)
        mock_self.real_trvs["climate.test_trv"]["model_quirks"].override_set_valve = (
            override_func
        )

        result = await set_valve(mock_self, "climate.test_trv", 50)

        assert result is True
        override_func.assert_called_once_with(mock_self, "climate.test_trv", 50)
        # Regular adapter set_valve should not be called
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_valve.assert_not_called()

    @pytest.mark.anyio
    async def test_uses_adapter_when_valve_writable(self, mock_self):
        """Test that adapter set_valve is used when valve is writable."""
        mock_self.real_trvs["climate.test_trv"]["model_quirks"].override_set_valve = None

        result = await set_valve(mock_self, "climate.test_trv", 75)

        assert result is True
        mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_valve.assert_called_once_with(mock_self, "climate.test_trv", 75)

    @pytest.mark.anyio
    async def test_returns_false_when_valve_not_writable(self, mock_self):
        """Test that function returns False when valve is not writable."""
        mock_self.real_trvs["climate.test_trv"]["valve_position_writable"] = False
        mock_self.real_trvs["climate.test_trv"]["model_quirks"].override_set_valve = None

        result = await set_valve(mock_self, "climate.test_trv", 50)

        assert result is False
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_valve.assert_not_called()

    @pytest.mark.anyio
    async def test_updates_last_valve_percent_when_override_used(self, mock_self):
        """Test that last_valve_percent is updated when override is used."""
        override_func = AsyncMock(return_value=True)
        mock_self.real_trvs["climate.test_trv"]["model_quirks"].override_set_valve = (
            override_func
        )

        await set_valve(mock_self, "climate.test_trv", 60)

        assert mock_self.real_trvs["climate.test_trv"]["last_valve_percent"] == 60
        assert mock_self.real_trvs["climate.test_trv"]["last_valve_method"] == "override"

    @pytest.mark.anyio
    async def test_updates_last_valve_percent_when_adapter_used(self, mock_self):
        """Test that last_valve_percent is updated when adapter is used."""
        mock_self.real_trvs["climate.test_trv"]["model_quirks"].override_set_valve = None

        await set_valve(mock_self, "climate.test_trv", 80)

        assert mock_self.real_trvs["climate.test_trv"]["last_valve_percent"] == 80
        assert mock_self.real_trvs["climate.test_trv"]["last_valve_method"] == "adapter"

    @pytest.mark.anyio
    async def test_handles_exception_gracefully(self, mock_self):
        """Test that function handles exceptions gracefully and returns False."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_valve = AsyncMock(
            side_effect=Exception("Failed")
        )
        mock_self.real_trvs["climate.test_trv"]["model_quirks"].override_set_valve = None

        result = await set_valve(mock_self, "climate.test_trv", 50)

        assert result is False


class TestSetOffset:
    """Test set_offset function."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.real_trvs = {
            "climate.test_trv": {
                "adapter": MagicMock(),
            }
        }
        mock.real_trvs["climate.test_trv"]["adapter"].set_offset = AsyncMock(
            return_value=1.5
        )
        return mock

    @pytest.mark.anyio
    async def test_calls_adapter_set_offset(self, mock_self):
        """Test that adapter set_offset is called."""
        result = await set_offset(mock_self, "climate.test_trv", 1.5)

        assert result == 1.5
        mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_offset.assert_called_once_with(mock_self, "climate.test_trv", 1.5)

    @pytest.mark.anyio
    async def test_returns_none_on_exception(self, mock_self):
        """Test that function returns None when exception occurs."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_offset = AsyncMock(
            side_effect=Exception("Failed")
        )

        result = await set_offset(mock_self, "climate.test_trv", 1.5)

        assert result is None


class TestDelegatedFunctions:
    """Test delegated functions that use @async_retry decorator."""

    @pytest.fixture
    def mock_self(self):
        """Create a mock BetterThermostat instance with adapter."""
        mock = MagicMock()
        mock.device_name = "Test Thermostat"
        mock.real_trvs = {
            "climate.test_trv": {
                "adapter": MagicMock(),
            }
        }
        return mock

    @pytest.mark.anyio
    async def test_init_delegates_to_adapter(self, mock_self):
        """Test that init delegates to adapter."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].init = AsyncMock(
            return_value=None
        )

        result = await init(mock_self, "climate.test_trv")

        mock_self.real_trvs["climate.test_trv"]["adapter"].init.assert_called_once_with(
            mock_self, "climate.test_trv"
        )

    @pytest.mark.anyio
    async def test_get_info_delegates_to_adapter(self, mock_self):
        """Test that get_info delegates to adapter."""
        expected_info = {"support_offset": True, "support_valve": False}
        mock_self.real_trvs["climate.test_trv"]["adapter"].get_info = AsyncMock(
            return_value=expected_info
        )

        result = await get_info(mock_self, "climate.test_trv")

        assert result == expected_info

    @pytest.mark.anyio
    async def test_get_current_offset_delegates_to_adapter(self, mock_self):
        """Test that get_current_offset delegates to adapter."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].get_current_offset = (
            AsyncMock(return_value=1.5)
        )

        result = await get_current_offset(mock_self, "climate.test_trv")

        assert result == 1.5

    @pytest.mark.anyio
    async def test_get_offset_step_delegates_to_adapter(self, mock_self):
        """Test that get_offset_step delegates to adapter."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].get_offset_step = AsyncMock(
            return_value=0.5
        )

        result = await get_offset_step(mock_self, "climate.test_trv")

        assert result == 0.5

    @pytest.mark.anyio
    async def test_get_min_offset_delegates_to_adapter(self, mock_self):
        """Test that get_min_offset delegates to adapter."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].get_min_offset = AsyncMock(
            return_value=-5.0
        )

        result = await get_min_offset(mock_self, "climate.test_trv")

        assert result == -5.0

    @pytest.mark.anyio
    async def test_get_max_offset_delegates_to_adapter(self, mock_self):
        """Test that get_max_offset delegates to adapter."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].get_max_offset = AsyncMock(
            return_value=5.0
        )

        result = await get_max_offset(mock_self, "climate.test_trv")

        assert result == 5.0

    @pytest.mark.anyio
    async def test_set_hvac_mode_delegates_to_adapter(self, mock_self):
        """Test that set_hvac_mode delegates to adapter."""
        mock_self.real_trvs["climate.test_trv"]["adapter"].set_hvac_mode = AsyncMock()

        await set_hvac_mode(mock_self, "climate.test_trv", "heat")

        mock_self.real_trvs["climate.test_trv"][
            "adapter"
        ].set_hvac_mode.assert_called_once_with(mock_self, "climate.test_trv", "heat")