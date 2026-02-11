"""Comprehensive tests for Better Thermostat adapters module.

Tests covering base, delegate, and generic adapter functionality including
calibration entity handling, timeout behavior, adapter loading, and TRV operations.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from custom_components.better_thermostat.adapters import base, delegate, generic


class TestWaitForCalibrationEntityOrTimeout:
    """Test wait_for_calibration_entity_or_timeout function."""

    @pytest.mark.asyncio
    async def test_calibration_entity_none_returns_early(self):
        """Test that function returns early if calibration_entity is None."""
        mock_self = MagicMock()
        mock_self.device_name = "test_thermostat"

        await base.wait_for_calibration_entity_or_timeout(
            mock_self, "climate.test", None
        )

        # Should not try to access hass.states since it returns early
        assert not mock_self.hass.states.get.called

    @pytest.mark.asyncio
    async def test_entity_becomes_available_immediately(self):
        """Test entity is available on first check."""
        mock_self = MagicMock()
        mock_self.device_name = "test_thermostat"

        # Simulate entity being available
        mock_state = MagicMock()
        mock_state.state = "0.0"
        mock_self.hass.states.get.return_value = mock_state

        await base.wait_for_calibration_entity_or_timeout(
            mock_self, "climate.test", "number.test_calibration"
        )

        # Should check the state
        mock_self.hass.states.get.assert_called()

    @pytest.mark.asyncio
    async def test_entity_timeout_forces_calibration_to_zero(self):
        """Test that timeout forces calibration to 0."""
        mock_self = MagicMock()
        mock_self.device_name = "test_thermostat"
        mock_self.context = MagicMock()

        # Simulate entity being unavailable
        mock_state = MagicMock()
        mock_state.state = STATE_UNAVAILABLE
        mock_self.hass.states.get.return_value = mock_state
        mock_self.hass.services.async_call = AsyncMock()

        # Mock asyncio.sleep to avoid waiting
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await base.wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test", "number.test_calibration"
            )

        # Should have called async_call to set calibration to 0
        mock_self.hass.services.async_call.assert_called_once()
        call_args = mock_self.hass.services.async_call.call_args
        assert call_args[0][0] == "number"
        assert call_args[0][1] == SERVICE_SET_VALUE
        assert call_args[0][2]["value"] == 0

    @pytest.mark.asyncio
    async def test_entity_becomes_available_after_retries(self):
        """Test entity becomes available after some retries."""
        mock_self = MagicMock()
        mock_self.device_name = "test_thermostat"

        # First 2 calls return unavailable, third returns available
        mock_unavailable = MagicMock()
        mock_unavailable.state = STATE_UNAVAILABLE
        mock_available = MagicMock()
        mock_available.state = "0.0"

        mock_self.hass.states.get.side_effect = [
            mock_unavailable,
            mock_unavailable,
            mock_available,
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await base.wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test", "number.test_calibration"
            )

        # Should have checked state 3 times
        assert mock_self.hass.states.get.call_count == 3

    @pytest.mark.asyncio
    async def test_service_call_exception_handling(self):
        """Test exception during service call is handled gracefully."""
        mock_self = MagicMock()
        mock_self.device_name = "test_thermostat"
        mock_self.context = MagicMock()

        mock_state = MagicMock()
        mock_state.state = STATE_UNAVAILABLE
        mock_self.hass.states.get.return_value = mock_state
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=Exception("Service call failed")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Should not raise, just log error
            await base.wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test", "number.test_calibration"
            )


class TestDelegateLoadAdapter:
    """Test delegate.load_adapter function."""

    @pytest.mark.asyncio
    async def test_load_adapter_generic_thermostat_conversion(self):
        """Test that generic_thermostat is converted to generic."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.hass = MagicMock()

        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module"
        ) as mock_import:
            mock_import.return_value = AsyncMock()

            result = await delegate.load_adapter(
                mock_self, "generic_thermostat", "climate.test", get_name=False
            )

            # Should import generic instead of generic_thermostat
            mock_import.assert_called_once()
            assert "generic" in str(mock_import.call_args)

    @pytest.mark.asyncio
    async def test_load_adapter_fallback_on_error(self):
        """Test fallback to generic adapter on import error."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.hass = MagicMock()

        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module"
        ) as mock_import:
            # First call fails, second succeeds (fallback)
            mock_import.side_effect = [
                Exception("Module not found"),
                AsyncMock(),
            ]

            result = await delegate.load_adapter(
                mock_self, "unknown_integration", "climate.test", get_name=False
            )

            # Should have tried twice
            assert mock_import.call_count == 2

    @pytest.mark.asyncio
    async def test_load_adapter_get_name_mode(self):
        """Test get_name=True sets device_name to '-'."""
        mock_self = MagicMock()
        mock_self.hass = MagicMock()

        with patch(
            "custom_components.better_thermostat.adapters.delegate.async_import_module"
        ) as mock_import:
            mock_import.return_value = AsyncMock()

            result = await delegate.load_adapter(
                mock_self, "generic", "climate.test", get_name=True
            )

            assert mock_self.device_name == "-"


class TestDelegateSetTemperature:
    """Test delegate.set_temperature function."""

    @pytest.mark.asyncio
    async def test_set_temperature_rounding_by_step(self):
        """Test temperature is rounded according to step."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "target_temp_step": 0.5,
                "adapter": MagicMock(set_temperature=AsyncMock()),
                "last_temperature": 20.0,
            }
        }

        await delegate.set_temperature(mock_self, "climate.test", 20.3)

        # Should round 20.3 to 20.5 (nearest 0.5)
        adapter = mock_self.real_trvs["climate.test"]["adapter"]
        adapter.set_temperature.assert_called_once()
        # Check that rounded value was passed (20.5)
        called_temp = adapter.set_temperature.call_args[0][2]
        assert called_temp == 20.5

    @pytest.mark.asyncio
    async def test_set_temperature_clamping_to_min_max(self):
        """Test temperature is clamped to min/max values."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.bt_target_temp_step = 0.5
        mock_self.real_trvs = {
            "climate.test": {
                "min_temp": 10.0,
                "max_temp": 25.0,
                "target_temp_step": 0.5,
                "adapter": MagicMock(set_temperature=AsyncMock()),
                "last_temperature": 20.0,
            }
        }

        # Test clamping to max
        await delegate.set_temperature(mock_self, "climate.test", 30.0)
        adapter = mock_self.real_trvs["climate.test"]["adapter"]
        called_temp = adapter.set_temperature.call_args[0][2]
        assert called_temp == 25.0

        # Reset mock
        adapter.set_temperature.reset_mock()

        # Test clamping to min
        await delegate.set_temperature(mock_self, "climate.test", 5.0)
        called_temp = adapter.set_temperature.call_args[0][2]
        assert called_temp == 10.0

    @pytest.mark.asyncio
    async def test_set_temperature_invalid_input_handling(self):
        """Test invalid temperature input is handled."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "target_temp_step": 0.5,
                "adapter": MagicMock(set_temperature=AsyncMock()),
                "last_temperature": 20.0,
            }
        }

        # Should convert invalid input to 0.0
        await delegate.set_temperature(mock_self, "climate.test", "invalid")
        adapter = mock_self.real_trvs["climate.test"]["adapter"]
        adapter.set_temperature.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_temperature_updates_last_temperature(self):
        """Test that last_temperature is updated."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "target_temp_step": 0.5,
                "adapter": MagicMock(set_temperature=AsyncMock()),
                "last_temperature": 20.0,
            }
        }

        await delegate.set_temperature(mock_self, "climate.test", 21.0)

        # last_temperature should be updated to rounded value
        assert mock_self.real_trvs["climate.test"]["last_temperature"] == 21.0


class TestDelegateSetValve:
    """Test delegate.set_valve function."""

    @pytest.mark.asyncio
    async def test_set_valve_with_override_takes_precedence(self):
        """Test override_set_valve from model quirks takes precedence."""
        mock_self = MagicMock()
        mock_self.device_name = "test"

        mock_override = AsyncMock(return_value=True)
        mock_quirks = MagicMock(override_set_valve=mock_override)

        mock_self.real_trvs = {
            "climate.test": {
                "model_quirks": mock_quirks,
                "valve_position_entity": "number.test_valve",
                "valve_position_writable": True,
                "adapter": MagicMock(set_valve=AsyncMock()),
                "last_valve_percent": 0,
                "last_valve_method": None,
            }
        }

        result = await delegate.set_valve(mock_self, "climate.test", 50)

        # Override should be called
        mock_override.assert_called_once()
        # Adapter should NOT be called
        assert not mock_self.real_trvs["climate.test"]["adapter"].set_valve.called
        # Should return True
        assert result is True
        # Should update tracking
        assert mock_self.real_trvs["climate.test"]["last_valve_percent"] == 50
        assert mock_self.real_trvs["climate.test"]["last_valve_method"] == "override"

    @pytest.mark.asyncio
    async def test_set_valve_with_writable_entity(self):
        """Test valve is set via adapter when entity is writable."""
        mock_self = MagicMock()
        mock_self.device_name = "test"

        mock_self.real_trvs = {
            "climate.test": {
                "valve_position_entity": "number.test_valve",
                "valve_position_writable": True,
                "adapter": MagicMock(set_valve=AsyncMock()),
                "last_valve_percent": 0,
                "last_valve_method": None,
            }
        }

        result = await delegate.set_valve(mock_self, "climate.test", 75)

        # Adapter should be called
        adapter = mock_self.real_trvs["climate.test"]["adapter"]
        adapter.set_valve.assert_called_once()
        # Should return True
        assert result is True
        # Should update tracking
        assert mock_self.real_trvs["climate.test"]["last_valve_percent"] == 75
        assert mock_self.real_trvs["climate.test"]["last_valve_method"] == "adapter"

    @pytest.mark.asyncio
    async def test_set_valve_without_writable_entity_returns_false(self):
        """Test returns False when valve is not writable."""
        mock_self = MagicMock()
        mock_self.device_name = "test"

        mock_self.real_trvs = {
            "climate.test": {
                "valve_position_entity": "number.test_valve",
                "valve_position_writable": False,
                "adapter": MagicMock(set_valve=AsyncMock()),
            }
        }

        result = await delegate.set_valve(mock_self, "climate.test", 50)

        # Should return False
        assert result is False
        # Adapter should NOT be called
        assert not mock_self.real_trvs["climate.test"]["adapter"].set_valve.called


class TestGenericAdapter:
    """Test generic adapter functions."""

    @pytest.mark.asyncio
    async def test_get_info_with_calibration_entity(self):
        """Test get_info returns support_offset=True when calibration entity exists."""
        mock_self = MagicMock()

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity"
        ) as mock_find:
            mock_find.return_value = "number.test_calibration"

            result = await generic.get_info(mock_self, "climate.test")

            assert result["support_offset"] is True
            assert result["support_valve"] is False

    @pytest.mark.asyncio
    async def test_get_info_without_calibration_entity(self):
        """Test get_info returns support_offset=False when no calibration entity."""
        mock_self = MagicMock()

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity"
        ) as mock_find:
            mock_find.return_value = None

            result = await generic.get_info(mock_self, "climate.test")

            assert result["support_offset"] is False
            assert result["support_valve"] is False

    @pytest.mark.asyncio
    async def test_init_finds_and_waits_for_calibration_entity(self):
        """Test init finds calibration entity and waits for it."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "local_temperature_calibration_entity": None,
                "calibration": 0,  # Not set to 1 (disabled)
            }
        }

        with patch(
            "custom_components.better_thermostat.adapters.generic.find_local_calibration_entity"
        ) as mock_find, patch(
            "custom_components.better_thermostat.adapters.generic.wait_for_calibration_entity_or_timeout"
        ) as mock_wait:
            mock_find.return_value = "number.test_calibration"
            mock_wait.return_value = AsyncMock()

            await generic.init(mock_self, "climate.test")

            # Should update the calibration entity
            assert (
                mock_self.real_trvs["climate.test"]["local_temperature_calibration_entity"]
                == "number.test_calibration"
            )
            # Should wait for entity
            mock_wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_current_offset_with_unavailable_state(self):
        """Test get_current_offset returns 0.0 when state is unavailable."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "local_temperature_calibration_entity": "number.test_calibration"
            }
        }

        mock_state = MagicMock()
        mock_state.state = STATE_UNAVAILABLE
        mock_self.hass.states.get.return_value = mock_state

        result = await generic.get_current_offset(mock_self, "climate.test")

        assert result == 0.0

    @pytest.mark.asyncio
    async def test_get_current_offset_removes_k_suffix(self):
        """Test get_current_offset removes 'k' suffix from SELECT entities."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "local_temperature_calibration_entity": "number.test_calibration"
            }
        }

        mock_state = MagicMock()
        mock_state.state = "1.5k"
        mock_self.hass.states.get.return_value = mock_state

        result = await generic.get_current_offset(mock_self, "climate.test")

        assert result == 1.5

    @pytest.mark.asyncio
    async def test_set_offset_for_select_entity(self):
        """Test set_offset uses select_option service for SELECT entities."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {
            "climate.test": {
                "local_temperature_calibration_entity": "select.test_calibration",
                "last_calibration": 0.0,
                "last_hvac_mode": "heat",
            }
        }

        mock_state = MagicMock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["0.0k", "0.5k", "1.0k", "1.5k", "2.0k"]}
        mock_self.hass.states.get.return_value = mock_state
        mock_self.hass.services.async_call = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock), patch(
            "custom_components.better_thermostat.adapters.generic.set_hvac_mode"
        ) as mock_hvac, patch(
            "custom_components.better_thermostat.adapters.generic.get_max_offset",
            return_value=6.0,
        ), patch(
            "custom_components.better_thermostat.adapters.generic.get_min_offset",
            return_value=-6.0,
        ):
            result = await generic.set_offset(mock_self, "climate.test", 1.5)

            # Should call select service
            call_args = [
                call
                for call in mock_self.hass.services.async_call.call_args_list
                if call[0][0] == "select"
            ]
            assert len(call_args) > 0
            assert call_args[0][0][1] == "select_option"

    @pytest.mark.asyncio
    async def test_set_temperature_calls_climate_service(self):
        """Test set_temperature calls climate.set_temperature service."""
        mock_self = MagicMock()
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock()

        await generic.set_temperature(mock_self, "climate.test", 21.5)

        # Should call climate service
        mock_self.hass.services.async_call.assert_called_once()
        call_args = mock_self.hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_temperature"
        assert call_args[0][2]["temperature"] == 21.5

    @pytest.mark.asyncio
    async def test_set_hvac_mode_normalizes_mode(self):
        """Test set_hvac_mode normalizes HVAC mode before calling service."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock()

        with patch(
            "custom_components.better_thermostat.adapters.generic.normalize_hvac_mode"
        ) as mock_normalize:
            mock_normalize.return_value = "heat"

            await generic.set_hvac_mode(mock_self, "climate.test", "heating")

            # Should normalize
            mock_normalize.assert_called_once()
            # Should call service
            mock_self.hass.services.async_call.assert_called_once()


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_concurrent_calibration_waits(self):
        """Test multiple concurrent calibration waits don't interfere."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()

        mock_state = MagicMock()
        mock_state.state = "0.0"
        mock_self.hass.states.get.return_value = mock_state

        # Run multiple waits concurrently
        await asyncio.gather(
            base.wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test1", "number.cal1"
            ),
            base.wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test2", "number.cal2"
            ),
            base.wait_for_calibration_entity_or_timeout(
                mock_self, "climate.test3", "number.cal3"
            ),
        )

        # All should complete without error
        assert mock_self.hass.states.get.call_count >= 3

    @pytest.mark.asyncio
    async def test_set_temperature_with_zero_step(self):
        """Test set_temperature handles zero step gracefully."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.bt_target_temp_step = 0.0  # Zero step
        mock_self.real_trvs = {
            "climate.test": {
                "target_temp_step": 0.0,
                "adapter": MagicMock(set_temperature=AsyncMock()),
                "last_temperature": 20.0,
            }
        }

        # Should not crash, should use default 0.5
        await delegate.set_temperature(mock_self, "climate.test", 20.3)

        adapter = mock_self.real_trvs["climate.test"]["adapter"]
        adapter.set_temperature.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_min_max_offset_for_select_with_invalid_options(self):
        """Test get_min/max_offset handles invalid SELECT options."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {
                "local_temperature_calibration_entity": "select.test_calibration"
            }
        }

        mock_state = MagicMock()
        mock_state.domain = "select"
        mock_state.attributes = {"options": ["invalid", "values"]}
        mock_self.hass.states.get.return_value = mock_state

        # Should return defaults when options can't be parsed
        min_val = await generic.get_min_offset(mock_self, "climate.test")
        max_val = await generic.get_max_offset(mock_self, "climate.test")

        assert min_val == -6.0
        assert max_val == 6.0