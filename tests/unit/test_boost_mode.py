"""Tests for boost mode valve control in control_trv function.

Boost mode sets valve to 100% and temperature to max_temp when:
- preset_mode is PRESET_BOOST
- cur_temp < bt_target_temp (heating is needed)

Safety override resets valve to 0% when HVAC mode is forced to OFF
(window open or call_for_heat=False) during active boost mode.
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import STATE_UNAVAILABLE
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import control_trv


class TestBoostModeUnavailablePath:
    """Test boost mode valve control when TRV is unavailable."""

    @pytest.mark.asyncio
    async def test_boost_mode_sets_valve_to_100(self):
        """Test that boost mode sets valve to 100% for unavailable TRV."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.preset_mode = PRESET_BOOST
        mock_self.cur_temp = 18.0
        mock_self.bt_target_temp = 22.0
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.cooler_entity_id = None
        mock_self.bt_target_cooltemp = 25.0
        mock_self.tolerance = 0.5
        mock_self.calculate_heating_power = AsyncMock()
        mock_adapter = Mock()
        mock_adapter.set_hvac_mode = AsyncMock(return_value=None)
        mock_adapter.set_temperature = AsyncMock(return_value=None)
        mock_adapter.set_valve = AsyncMock(return_value=True)
        mock_adapter.get_current_offset = AsyncMock(return_value=0.0)

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "min_temp": 5.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "adapter": mock_adapter,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.DIRECT_VALVE_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
            }
        }

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve"
            ) as mock_set_valve,
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "local_temperature_calibration": 0.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_valve.return_value = True

            result = await control_trv(mock_self, "climate.trv1")

            # Should call set_valve with 100%
            mock_set_valve.assert_called_once()
            args = mock_set_valve.call_args[0]
            assert args[2] == 100  # 100% valve

            assert result is True

    @pytest.mark.asyncio
    async def test_boost_mode_sets_max_temp(self):
        """Test that boost mode sets temperature to max_temp."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE
        mock_state.attributes = {"temperature": 15.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state
        mock_hass.services = Mock()
        mock_hass.services.async_call = AsyncMock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.preset_mode = PRESET_BOOST
        mock_self.cur_temp = 18.0
        mock_self.bt_target_temp = 22.0
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.cooler_entity_id = None
        mock_self.bt_target_cooltemp = 25.0
        mock_self.tolerance = 0.5
        mock_self.context = None
        mock_self.calculate_heating_power = AsyncMock()

        mock_adapter = Mock()
        mock_adapter.set_hvac_mode = AsyncMock(return_value=None)
        mock_adapter.set_temperature = AsyncMock(return_value=None)
        mock_adapter.set_valve = AsyncMock(return_value=True)
        mock_adapter.get_current_offset = AsyncMock(return_value=0.0)

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "min_temp": 5.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "last_temperature": 20.0,
                "temperature": 20.0,
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "adapter": mock_adapter,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
            }
        }

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature"
            ) as mock_set_temp,
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            result = await control_trv(mock_self, "climate.trv1")

            # Should call set_temperature with max_temp (30.0)
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 30.0  # max_temp

            assert result is True


class TestBoostModeAvailablePath:
    """Test boost mode valve control when TRV is available."""

    @pytest.mark.asyncio
    async def test_boost_mode_sets_valve_100_and_max_temp(self):
        """Test that boost mode sets valve to 100% and temperature to max_temp.

        When preset_mode is BOOST with DIRECT_VALVE_BASED calibration:
        - Valve should be set to 100%
        - Temperature should be set to max_temp
        """
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.preset_mode = PRESET_BOOST
        mock_self.cur_temp = 18.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.cooler_entity_id = None
        mock_self.bt_target_cooltemp = 25.0
        mock_self.tolerance = 0.5
        mock_self.calculate_heating_power = AsyncMock()

        mock_adapter = Mock()
        mock_adapter.set_hvac_mode = AsyncMock(return_value=None)
        mock_adapter.set_temperature = AsyncMock(return_value=None)
        mock_adapter.set_valve = AsyncMock(return_value=True)
        mock_adapter.get_current_offset = AsyncMock(return_value=0.0)

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "temperature": 20.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "adapter": mock_adapter,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.DIRECT_VALVE_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
            }
        }

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve"
            ) as mock_set_valve,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_temperature"
            ) as mock_set_temp,
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_valve.return_value = True
            mock_set_temp.return_value = AsyncMock()

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            mock_set_valve.assert_called_once()
            args = mock_set_valve.call_args[0]
            assert args[2] == 100

            mock_set_temp.assert_called_once()
            temp_args = mock_set_temp.call_args[0]
            assert temp_args[2] == 30.0


class TestBoostModeSafetyOverride:
    """Test safety override resets valve when HVAC is forced to OFF during boost mode."""

    @pytest.mark.asyncio
    async def test_window_open_resets_valve_during_boost(self):
        """Test that window open resets valve to 0% when boost mode was active.

        Scenario:
        1. Boost mode sets valve to 100%
        2. Window opens (window_open=True) which forces HVAC mode to OFF
        3. Valve should be reset to 0% to avoid valve 100% + HVAC OFF conflict
        """
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.preset_mode = PRESET_BOOST
        mock_self.cur_temp = 18.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = True  # Window is OPEN
        mock_self.call_for_heat = True
        mock_self.cooler_entity_id = None
        mock_self.calculate_heating_power = AsyncMock()

        # Prevent 'coroutine was never awaited' warning
        def _close_coro(coro):
            if inspect.iscoroutine(coro):
                coro.close()
            return Mock()

        mock_self.task_manager = Mock()
        mock_self.task_manager.create_task = Mock(side_effect=_close_coro)

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "temperature": 20.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.DIRECT_VALVE_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": True,
                "target_temp_received": False,
                "calibration_received": False,
                "last_hvac_mode": HVACMode.HEAT,
            }
        }

        set_valve_calls = []

        async def track_set_valve(*args, **kwargs):
            set_valve_calls.append(args)
            return True

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                side_effect=track_set_valve,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
            ),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

            # First call: boost mode sets valve to 100%
            # Second call: safety override resets valve to 0%
            assert len(set_valve_calls) == 2
            assert set_valve_calls[0][2] == 100  # Boost: 100%
            assert set_valve_calls[1][2] == 0  # Safety reset: 0%

    @pytest.mark.asyncio
    async def test_no_heat_call_resets_valve_during_boost(self):
        """Test that call_for_heat=False resets valve to 0% when boost mode was active.

        Scenario:
        1. Boost mode sets valve to 100%
        2. call_for_heat becomes False which forces HVAC mode to OFF
        3. Valve should be reset to 0% to avoid valve 100% + HVAC OFF conflict
        """
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.preset_mode = PRESET_BOOST
        mock_self.cur_temp = 18.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = False  # No heat call
        mock_self.cooler_entity_id = None
        mock_self.calculate_heating_power = AsyncMock()

        # Prevent 'coroutine was never awaited' warning
        def _close_coro(coro):
            if inspect.iscoroutine(coro):
                coro.close()
            return Mock()

        mock_self.task_manager = Mock()
        mock_self.task_manager.create_task = Mock(side_effect=_close_coro)

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "temperature": 20.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.DIRECT_VALVE_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": True,
                "target_temp_received": False,
                "calibration_received": False,
                "last_hvac_mode": HVACMode.HEAT,
            }
        }

        set_valve_calls = []

        async def track_set_valve(*args, **kwargs):
            set_valve_calls.append(args)
            return True

        with (
            patch(
                "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
            ) as mock_convert,
            patch(
                "custom_components.better_thermostat.utils.controlling.set_valve",
                side_effect=track_set_valve,
            ),
            patch(
                "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
            ),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

            # First call: boost mode sets valve to 100%
            # Second call: safety override resets valve to 0%
            assert len(set_valve_calls) == 2
            assert set_valve_calls[0][2] == 100  # Boost: 100%
            assert set_valve_calls[1][2] == 0  # Safety reset: 0%
