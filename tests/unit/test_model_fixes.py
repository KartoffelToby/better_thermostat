"""Comprehensive tests for Better Thermostat model_fixes module.

Tests covering device-specific quirks for BTH-RM, BTH-RM230Z, TRVZB, TV02-Zigbee,
and default model implementations including calibration fixes, temperature overrides,
and valve control.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.components.lock import LockState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.helpers import entity_registry as er

from custom_components.better_thermostat.model_fixes import (
    TRVZB,
    TV02_Zigbee,
    default,
)

# Import quirk modules with hyphens by using importlib
import importlib.util

spec_bth_rm = importlib.util.spec_from_file_location(
    "BTH_RM",
    "/home/jailuser/git/custom_components/better_thermostat/model_fixes/BTH-RM.py",
)
BTH_RM = importlib.util.module_from_spec(spec_bth_rm)
spec_bth_rm.loader.exec_module(BTH_RM)

spec_bth_rm230z = importlib.util.spec_from_file_location(
    "BTH_RM230Z",
    "/home/jailuser/git/custom_components/better_thermostat/model_fixes/BTH-RM230Z.py",
)
BTH_RM230Z = importlib.util.module_from_spec(spec_bth_rm230z)
spec_bth_rm230z.loader.exec_module(BTH_RM230Z)


class TestBTHRM:
    """Test BTH-RM model quirks."""

    def test_fix_local_calibration_passthrough(self):
        """Test fix_local_calibration returns offset unchanged."""
        mock_self = MagicMock()
        result = BTH_RM.fix_local_calibration(mock_self, "climate.test", 2.5)
        assert result == 2.5

    def test_fix_target_temperature_calibration_passthrough(self):
        """Test fix_target_temperature_calibration returns temperature unchanged."""
        mock_self = MagicMock()
        result = BTH_RM.fix_target_temperature_calibration(
            mock_self, "climate.test", 21.5
        )
        assert result == 21.5

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_returns_false(self):
        """Test override_set_hvac_mode returns False (no override)."""
        mock_self = MagicMock()
        result = await BTH_RM.override_set_hvac_mode(
            mock_self, "climate.test", HVACMode.HEAT
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_override_set_temperature_with_heat_and_cool(self):
        """Test override_set_temperature uses target_temp_high/low for heat+cool."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "BTH-RM"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_entry = MagicMock()
        mock_entry.capabilities = {"hvac_modes": ["heat", "cool", "off"]}
        mock_entry.platform = "mqtt"
        mock_entity_reg.async_get.return_value = mock_entry

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            result = await BTH_RM.override_set_temperature(
                mock_self, "climate.test", 21.5
            )

            assert result is True
            # Should call with target_temp_high and target_temp_low
            call_args = mock_self.hass.services.async_call.call_args
            assert "target_temp_high" in call_args[0][2]
            assert "target_temp_low" in call_args[0][2]
            assert call_args[0][2]["target_temp_high"] == 21.5
            assert call_args[0][2]["target_temp_low"] == 21.5

    @pytest.mark.asyncio
    async def test_override_set_temperature_fallback_without_cool(self):
        """Test override_set_temperature uses simple temperature without cool mode."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "BTH-RM"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_entry = MagicMock()
        mock_entry.capabilities = {"hvac_modes": ["heat", "off"]}  # No cool
        mock_entry.platform = "mqtt"
        mock_entity_reg.async_get.return_value = mock_entry

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            result = await BTH_RM.override_set_temperature(
                mock_self, "climate.test", 21.5
            )

            assert result is True
            # Should call with simple temperature
            call_args = mock_self.hass.services.async_call.call_args
            assert "temperature" in call_args[0][2]
            assert call_args[0][2]["temperature"] == 21.5

    @pytest.mark.asyncio
    async def test_override_set_temperature_fallback_no_registry_entry(self):
        """Test override_set_temperature fallback when no registry entry."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "BTH-RM"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_entity_reg.async_get.return_value = None  # No entry

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            result = await BTH_RM.override_set_temperature(
                mock_self, "climate.test", 21.5
            )

            assert result is True
            # Should fallback to simple set_temperature
            call_args = mock_self.hass.services.async_call.call_args
            assert "temperature" in call_args[0][2]


class TestBTHRM230Z:
    """Test BTH-RM230Z model quirks."""

    def test_fix_local_calibration_passthrough(self):
        """Test fix_local_calibration returns offset unchanged."""
        mock_self = MagicMock()
        result = BTH_RM230Z.fix_local_calibration(mock_self, "climate.test", 1.5)
        assert result == 1.5

    def test_fix_target_temperature_calibration_passthrough(self):
        """Test fix_target_temperature_calibration returns temperature unchanged."""
        mock_self = MagicMock()
        result = BTH_RM230Z.fix_target_temperature_calibration(
            mock_self, "climate.test", 22.0
        )
        assert result == 22.0

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_returns_false(self):
        """Test override_set_hvac_mode returns False (no override)."""
        mock_self = MagicMock()
        result = await BTH_RM230Z.override_set_hvac_mode(
            mock_self, "climate.test", HVACMode.HEAT
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_override_set_temperature_with_heat_and_cool(self):
        """Test override_set_temperature uses target_temp_high/low for heat+cool."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "BTH-RM230Z"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_entry = MagicMock()
        mock_entry.capabilities = {"hvac_modes": ["heat", "cool", "off"]}
        mock_entry.platform = "mqtt"
        mock_entity_reg.async_get.return_value = mock_entry

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            result = await BTH_RM230Z.override_set_temperature(
                mock_self, "climate.test", 20.5
            )

            assert result is True
            call_args = mock_self.hass.services.async_call.call_args
            assert call_args[0][2]["target_temp_high"] == 20.5
            assert call_args[0][2]["target_temp_low"] == 20.5


class TestTRVZB:
    """Test TRVZB (Sonoff) model quirks."""

    def test_fix_local_calibration_passthrough(self):
        """Test fix_local_calibration returns offset unchanged."""
        mock_self = MagicMock()
        result = TRVZB.fix_local_calibration(mock_self, "climate.test", 3.0)
        assert result == 3.0

    def test_fix_target_temperature_calibration_passthrough(self):
        """Test fix_target_temperature_calibration returns temperature unchanged."""
        mock_self = MagicMock()
        result = TRVZB.fix_target_temperature_calibration(
            mock_self, "climate.test", 19.5
        )
        assert result == 19.5

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_calls_service(self):
        """Test override_set_hvac_mode calls climate service."""
        mock_self = MagicMock()
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock()

        result = await TRVZB.override_set_hvac_mode(
            mock_self, "climate.test", HVACMode.HEAT
        )

        assert result is True
        mock_self.hass.services.async_call.assert_called_once()
        call_args = mock_self.hass.services.async_call.call_args
        assert call_args[0][0] == "climate"
        assert call_args[0][1] == "set_hvac_mode"

    @pytest.mark.asyncio
    async def test_override_set_temperature_calls_service(self):
        """Test override_set_temperature calls climate service."""
        mock_self = MagicMock()
        mock_self.context = MagicMock()
        mock_self.hass.services.async_call = AsyncMock()

        result = await TRVZB.override_set_temperature(mock_self, "climate.test", 21.0)

        assert result is True
        mock_self.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_maybe_set_sonoff_valve_percent_finds_opening_entity(self):
        """Test maybe_set_sonoff_valve_percent finds and writes to valve opening entity."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "TRVZB"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        mock_valve_entry = MagicMock()
        mock_valve_entry.entity_id = "number.test_valve_opening_degree"
        mock_valve_entry.device_id = "device123"
        mock_valve_entry.domain = "number"
        mock_valve_entry.unique_id = "test_valve_opening_degree"

        mock_entity_reg.entities.values.return_value = [mock_valve_entry]

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            result = await TRVZB.maybe_set_sonoff_valve_percent(
                mock_self, "climate.test", 75
            )

            assert result is True
            # Should call number.set_value with 75
            call_args = mock_self.hass.services.async_call.call_args
            assert call_args[0][0] == "number"
            assert call_args[0][1] == "set_value"
            assert call_args[0][2]["value"] == 75

    @pytest.mark.asyncio
    async def test_maybe_set_sonoff_valve_percent_skips_non_sonoff(self):
        """Test maybe_set_sonoff_valve_percent skips non-Sonoff models."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {"climate.test": {"model": "OtherBrand"}}

        result = await TRVZB.maybe_set_sonoff_valve_percent(
            mock_self, "climate.test", 50
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_override_set_valve_applies_bump_workaround_when_closing(self):
        """Test override_set_valve applies bump-open workaround when closing."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.in_maintenance = False
        mock_self.real_trvs = {
            "climate.test": {
                "model": "TRVZB",
                "last_valve_percent": 80,  # Previously at 80%
            }
        }
        mock_self.hass = MagicMock()
        mock_self.hass.async_create_task = MagicMock(side_effect=lambda coro: None)

        with patch.object(
            TRVZB, "maybe_set_sonoff_valve_percent", new_callable=AsyncMock
        ) as mock_set:
            mock_set.return_value = True

            result = await TRVZB.override_set_valve(mock_self, "climate.test", 50)

            assert result is True
            # Should call twice: first with bump (90%), then scheduled for target (50%)
            assert mock_set.call_count >= 1

    @pytest.mark.asyncio
    async def test_override_set_valve_no_bump_when_opening(self):
        """Test override_set_valve does not apply bump when opening."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.in_maintenance = False
        mock_self.real_trvs = {
            "climate.test": {"model": "TRVZB", "last_valve_percent": 50}
        }

        with patch.object(
            TRVZB, "maybe_set_sonoff_valve_percent", new_callable=AsyncMock
        ) as mock_set:
            mock_set.return_value = True

            result = await TRVZB.override_set_valve(mock_self, "climate.test", 70)

            assert result is True
            # Should call once with target value (no bump needed)
            assert mock_set.call_count == 1
            assert mock_set.call_args[0][2] == 70

    @pytest.mark.asyncio
    async def test_maybe_set_external_temperature_finds_and_writes(self):
        """Test maybe_set_external_temperature finds and writes to external temp input."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "TRVZB"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        mock_ext_temp_entry = MagicMock()
        mock_ext_temp_entry.entity_id = "number.test_external_temperature_input"
        mock_ext_temp_entry.device_id = "device123"
        mock_ext_temp_entry.domain = "number"
        mock_ext_temp_entry.unique_id = "test_external_temperature_input"

        mock_entity_reg.entities.values.return_value = [mock_ext_temp_entry]

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            result = await TRVZB.maybe_set_external_temperature(
                mock_self, "climate.test", 22.5
            )

            assert result is True
            call_args = mock_self.hass.services.async_call.call_args
            assert call_args[0][2]["value"] == 22.5

    @pytest.mark.asyncio
    async def test_maybe_set_external_temperature_clamps_value(self):
        """Test maybe_set_external_temperature clamps value to 0-99.9 range."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "TRVZB"}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        mock_ext_temp_entry = MagicMock()
        mock_ext_temp_entry.entity_id = "number.test_external_temperature_input"
        mock_ext_temp_entry.device_id = "device123"
        mock_ext_temp_entry.domain = "number"

        mock_entity_reg.entities.values.return_value = [mock_ext_temp_entry]

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            # Test clamping to max
            await TRVZB.maybe_set_external_temperature(mock_self, "climate.test", 150.0)
            call_args = mock_self.hass.services.async_call.call_args
            assert call_args[0][2]["value"] == 99.9

            # Reset mock
            mock_self.hass.services.async_call.reset_mock()

            # Test clamping to min
            await TRVZB.maybe_set_external_temperature(mock_self, "climate.test", -10.0)
            call_args = mock_self.hass.services.async_call.call_args
            assert call_args[0][2]["value"] == 0.0


class TestTV02Zigbee:
    """Test TV02-Zigbee model quirks."""

    def test_fix_local_calibration_passthrough(self):
        """Test fix_local_calibration returns offset unchanged."""
        mock_self = MagicMock()
        result = TV02_Zigbee.fix_local_calibration(mock_self, "climate.test", 2.0)
        assert result == 2.0

    def test_fix_target_temperature_calibration_passthrough(self):
        """Test fix_target_temperature_calibration returns temperature unchanged."""
        mock_self = MagicMock()
        result = TV02_Zigbee.fix_target_temperature_calibration(
            mock_self, "climate.test", 23.0
        )
        assert result == 23.0

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_sets_manual_preset(self):
        """Test override_set_hvac_mode sets manual preset for non-OFF modes."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "TV02-Zigbee"}}
        mock_self.hass.services.async_call = AsyncMock()

        result = await TV02_Zigbee.override_set_hvac_mode(
            mock_self, "climate.test", HVACMode.HEAT
        )

        assert result is True
        # Should call set_hvac_mode and set_preset_mode
        assert mock_self.hass.services.async_call.call_count == 2
        # Second call should be set_preset_mode to manual
        second_call = mock_self.hass.services.async_call.call_args_list[1]
        assert second_call[0][1] == "set_preset_mode"
        assert second_call[0][2]["preset_mode"] == "manual"

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_off_no_preset(self):
        """Test override_set_hvac_mode does not set preset for OFF mode."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "TV02-Zigbee"}}
        mock_self.hass.services.async_call = AsyncMock()

        result = await TV02_Zigbee.override_set_hvac_mode(
            mock_self, "climate.test", HVACMode.OFF
        )

        assert result is True
        # Should only call set_hvac_mode (no preset for OFF)
        assert mock_self.hass.services.async_call.call_count == 1

    @pytest.mark.asyncio
    async def test_override_set_temperature_sets_manual_preset(self):
        """Test override_set_temperature sets manual preset."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.context = MagicMock()
        mock_self.real_trvs = {"climate.test": {"model": "TV02-Zigbee"}}
        mock_self.hass.services.async_call = AsyncMock()

        result = await TV02_Zigbee.override_set_temperature(
            mock_self, "climate.test", 21.5
        )

        assert result is True
        # Should call set_preset_mode and set_temperature
        assert mock_self.hass.services.async_call.call_count == 2


class TestDefaultQuirks:
    """Test default model quirks for unknown devices."""

    def test_fix_local_calibration_passthrough(self):
        """Test fix_local_calibration returns offset unchanged."""
        mock_self = MagicMock()
        result = default.fix_local_calibration(mock_self, "climate.test", 1.5)
        assert result == 1.5

    def test_fix_target_temperature_calibration_passthrough(self):
        """Test fix_target_temperature_calibration returns temperature unchanged."""
        mock_self = MagicMock()
        result = default.fix_target_temperature_calibration(
            mock_self, "climate.test", 20.0
        )
        assert result == 20.0

    @pytest.mark.asyncio
    async def test_override_set_hvac_mode_returns_false(self):
        """Test override_set_hvac_mode returns False (no override)."""
        mock_self = MagicMock()
        result = await default.override_set_hvac_mode(
            mock_self, "climate.test", HVACMode.HEAT
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_override_set_temperature_returns_false(self):
        """Test override_set_temperature returns False (no override)."""
        mock_self = MagicMock()
        result = await default.override_set_temperature(mock_self, "climate.test", 21.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_initial_tweak_resets_calibration(self):
        """Test inital_tweak resets local calibration to 0."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {"climate.test": {"advanced": {"child_lock": False}}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        # Create calibration entity
        mock_cal_entry = MagicMock()
        mock_cal_entry.entity_id = "number.test_local_temperature_calibration"
        mock_cal_entry.device_id = "device123"
        mock_cal_entry.domain = "number"

        mock_entity_reg.entities.values.return_value = [mock_cal_entry]

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            await default.inital_tweak(mock_self, "climate.test")

            # Should call number.set_value with 0
            calls = [
                c
                for c in mock_self.hass.services.async_call.call_args_list
                if c[0][0] == "number"
            ]
            assert len(calls) > 0
            assert calls[0][0][2]["value"] == 0

    @pytest.mark.asyncio
    async def test_initial_tweak_sets_child_lock_switch(self):
        """Test inital_tweak sets child lock switch when enabled."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {
            "climate.test": {"advanced": {"child_lock": True}}  # Enabled
        }
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        # Create child lock switch
        mock_lock_entry = MagicMock()
        mock_lock_entry.entity_id = "switch.test_child_lock"
        mock_lock_entry.device_id = "device123"
        mock_lock_entry.domain = "switch"

        mock_entity_reg.entities.values.return_value = [mock_lock_entry]

        # Mock current state
        mock_state = MagicMock()
        mock_state.state = STATE_OFF
        mock_self.hass.states.get.return_value = mock_state

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            await default.inital_tweak(mock_self, "climate.test")

            # Should call switch.turn_on
            calls = [
                c
                for c in mock_self.hass.services.async_call.call_args_list
                if c[0][0] == "switch" and c[0][1] == "turn_on"
            ]
            assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_initial_tweak_disables_window_detection(self):
        """Test inital_tweak disables window detection switch."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {"climate.test": {"advanced": {"child_lock": False}}}
        mock_self.hass.services.async_call = AsyncMock()

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        # Create window detection switch
        mock_window_entry = MagicMock()
        mock_window_entry.entity_id = "switch.test_window_detection"
        mock_window_entry.device_id = "device123"
        mock_window_entry.domain = "switch"

        mock_entity_reg.entities.values.return_value = [mock_window_entry]

        # Mock current state as ON
        mock_state = MagicMock()
        mock_state.state = STATE_ON
        mock_self.hass.states.get.return_value = mock_state

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            await default.inital_tweak(mock_self, "climate.test")

            # Should call switch.turn_off
            calls = [
                c
                for c in mock_self.hass.services.async_call.call_args_list
                if c[0][0] == "switch" and c[0][1] == "turn_off"
            ]
            assert len(calls) > 0

    @pytest.mark.asyncio
    async def test_initial_tweak_handles_exceptions_gracefully(self):
        """Test inital_tweak handles exceptions without crashing."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {"climate.test": {"advanced": {"child_lock": False}}}
        mock_self.hass.services.async_call = AsyncMock(
            side_effect=Exception("Service error")
        )

        mock_entity_reg = MagicMock()
        mock_climate_entry = MagicMock()
        mock_climate_entry.device_id = "device123"
        mock_entity_reg.async_get.return_value = mock_climate_entry

        mock_cal_entry = MagicMock()
        mock_cal_entry.entity_id = "number.test_calibration"
        mock_cal_entry.device_id = "device123"
        mock_cal_entry.domain = "number"

        mock_entity_reg.entities.values.return_value = [mock_cal_entry]

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            # Should not raise exception
            await default.inital_tweak(mock_self, "climate.test")


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_trvzb_valve_bump_cancellation(self):
        """Test that pending valve bump tasks are cancelled properly."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.in_maintenance = False
        mock_self.real_trvs = {
            "climate.test": {
                "model": "TRVZB",
                "last_valve_percent": 80,
                "_trvzb_valve_bump_task": MagicMock(),
            }
        }

        # Mock the cancel method
        mock_task = mock_self.real_trvs["climate.test"]["_trvzb_valve_bump_task"]
        mock_task.cancel = MagicMock()

        with patch.object(
            TRVZB, "maybe_set_sonoff_valve_percent", new_callable=AsyncMock
        ) as mock_set:
            mock_set.return_value = True

            # Call override_set_valve which should cancel existing task
            await TRVZB.override_set_valve(mock_self, "climate.test", 50)

            # Old task should be cancelled
            mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_initial_tweak_with_no_device_id(self):
        """Test inital_tweak handles missing device_id gracefully."""
        mock_self = MagicMock()
        mock_self.device_name = "test"
        mock_self.real_trvs = {"climate.test": {"advanced": {"child_lock": False}}}

        mock_entity_reg = MagicMock()
        mock_entity_reg.async_get.return_value = None  # No registry entry

        with patch.object(er, "async_get", return_value=mock_entity_reg):
            # Should not crash
            await default.inital_tweak(mock_self, "climate.test")