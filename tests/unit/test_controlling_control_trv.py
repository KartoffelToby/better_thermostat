"""Tests for control_trv function in utils/controlling.py.

This is the most complex function in controlling.py with ~600 lines of code.
It has two main paths:
1. Unavailable TRV path (lines 263-582)
2. Available TRV path (lines 584-829)

These paths have ~95% code duplication, which is a code smell and potential bug source.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import control_trv


class TestControlTrvUnavailablePath:
    """Test control_trv function when TRV is unavailable.

    This tests lines 263-582 (unavailable TRV path).
    """

    @pytest.mark.asyncio
    async def test_trv_none_returns_true(self):
        """Test that None TRV returns True (no retry)."""
        mock_hass = Mock()
        mock_hass.states.get.return_value = None

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = {"temperature": 20.0, "system_mode": HVACMode.HEAT}

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            assert mock_self.real_trvs["climate.trv1"]["ignore_trv_states"] is False

    @pytest.mark.asyncio
    async def test_trv_unavailable_returns_true(self):
        """Test that unavailable TRV returns True (no retry)."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = {"temperature": 20.0, "system_mode": HVACMode.HEAT}

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_unavailable_trv_no_operations_called(self):
        """Test that unavailable TRVs skip all control operations.

        When a TRV is unavailable, control_trv should:
        - Return True immediately
        - Not call set_valve, set_hvac_mode, set_offset, or set_temperature
        - Not call convert_outbound_states

        This ensures unavailable TRVs don't trigger retries or state changes.
        """
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.preset_mode = None
        mock_self.call_for_heat = True
        mock_self.cooler_entity_id = None
        mock_self.window_open = False
        mock_self.task_manager = Mock(create_task=Mock())
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "temperature": 20.0,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
                "model_quirks": Mock(override_set_hvac_mode=AsyncMock(return_value=False)),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_valve"
        ) as mock_set_valve, patch(
            "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
        ) as mock_set_hvac_mode, patch(
            "custom_components.better_thermostat.utils.controlling.set_offset"
        ) as mock_set_offset, patch(
            "custom_components.better_thermostat.utils.controlling.set_temperature"
        ) as mock_set_temp, patch(
            "custom_components.better_thermostat.utils.controlling.handle_window_open"
        ) as mock_window:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
                "local_temperature_calibration": 0.0,
            }
            mock_window.return_value = HVACMode.OFF

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            mock_convert.assert_not_called()
            mock_set_valve.assert_not_called()
            mock_set_hvac_mode.assert_not_called()
            mock_set_offset.assert_not_called()
            mock_set_temp.assert_not_called()

    @pytest.mark.asyncio
    async def test_trv_unknown_returns_true(self):
        """Test that unknown TRV returns True (no retry)."""
        mock_state = Mock()
        mock_state.state = STATE_UNKNOWN

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = {"temperature": 20.0, "system_mode": HVACMode.HEAT}

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_convert_outbound_states_fails_returns_true(self):
        """Test that convert_outbound_states failure for unavailable TRV returns True.

        When a TRV is unavailable and convert_outbound_states fails:
        - control_trv should return True (no retry)
        - This prevents unnecessary retry loops
        - Consistent with normal unavailable TRV behavior
        """
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.ignore_states = False
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            # convert_outbound_states returns non-dict (error)
            mock_convert.return_value = "ERROR"

            result = await control_trv(mock_self, "climate.trv1")

            # Unavailable TRV should return True (no retry)
            assert result is True

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
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "min_temp": 5.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.DIRECT_VALVE_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_valve"
        ) as mock_set_valve:
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
        mock_self.context = None
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "min_temp": 5.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "last_temperature": 20.0,
                "temperature": 20.0,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_temperature"
        ) as mock_set_temp:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            await control_trv(mock_self, "climate.trv1")

            # Should call set_temperature with max_temp (30.0)
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 30.0  # max_temp

    @pytest.mark.asyncio
    async def test_window_open_sets_mode_to_off(self):
        """Test that window open sets HVAC mode to OFF."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.window_open = True
        mock_self.call_for_heat = True
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
        ) as mock_set_hvac, patch(
            "custom_components.better_thermostat.utils.controlling.override_set_hvac_mode"
        ) as mock_override:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False

            result = await control_trv(mock_self, "climate.trv1")

            # Window open should force mode to OFF via handle_window_open
            # set_hvac_mode should be called with OFF
            assert result is True
            mock_set_hvac.assert_called_once()
            assert mock_set_hvac.call_args[0][2] == HVACMode.OFF
            # Check that override was called with OFF mode
            mock_override.assert_called_once()
            override_args = mock_override.call_args[0]
            assert override_args[2] == HVACMode.OFF  # Mode should be OFF

    @pytest.mark.asyncio
    async def test_no_off_mode_sends_min_temp_when_off_requested(self):
        """Test that TRV without OFF mode sends min_temp when OFF is requested."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.window_open = False
        mock_self.call_for_heat = False  # No heat needed -> OFF
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT],  # No OFF mode!
                "min_temp": 5.0,
                "temperature": 20.0,
                "last_temperature": 20.0,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_temperature"
        ) as mock_set_temp:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            await control_trv(mock_self, "climate.trv1")

            # Should set temperature to min_temp (5.0) because OFF is not available
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 5.0  # min_temp

    @pytest.mark.asyncio
    async def test_ignore_trv_states_flag_set_and_reset(self):
        """Test that ignore_trv_states flag is set during processing and reset after."""
        mock_state = Mock()
        mock_state.state = STATE_UNAVAILABLE

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            # After completion, flag should be reset
            assert mock_self.real_trvs["climate.trv1"]["ignore_trv_states"] is False


class TestControlTrvAvailablePath:
    """Test control_trv function when TRV is available.

    This tests lines 584-829 (available TRV path).
    """

    @pytest.mark.asyncio
    async def test_available_trv_normal_operation(self):
        """Test normal operation with available TRV."""
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_available_trv_convert_fails_returns_false(self):
        """Test that convert failure returns False for available TRV."""
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = "ERROR"

            result = await control_trv(mock_self, "climate.trv1")

            assert result is False

    @pytest.mark.asyncio
    async def test_boost_mode_sets_valve_full_open(self):
        """Test that boost mode sets valve to 100% for direct valve control.

        When preset_mode is BOOST and calibration type is DIRECT_VALVE_BASED:
        - Valve should be set to 100%
        - Temperature should be set to max_temp
        - This applies to available TRVs
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
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "max_temp": 30.0,
                "temperature": 20.0,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.DIRECT_VALVE_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_valve"
        ) as mock_set_valve:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_valve.return_value = True

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            mock_set_valve.assert_called_once()
            args = mock_set_valve.call_args[0]
            assert args[2] == 100

    @pytest.mark.asyncio
    async def test_grouped_trv_calibration_fix(self):
        """Test grouped TRV calibration fix (lines 779-791).

        This fix is ONLY in available path, NOT in unavailable path!
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
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "last_calibration": 2.0,
                "calibration_received": False,  # Stuck at False
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.get_current_offset"
        ) as mock_get_offset:
            mock_convert.return_value = {
                "temperature": 20.0,
                "local_temperature_calibration": 2.0,
                "system_mode": HVACMode.HEAT,
            }
            # Current calibration already matches target
            mock_get_offset.return_value = 2.0

            result = await control_trv(mock_self, "climate.trv1")

            # The fix should reset calibration_received to True
            assert mock_self.real_trvs["climate.trv1"]["calibration_received"] is True
            assert result is True

    @pytest.mark.asyncio
    async def test_get_current_offset_none_returns_true(self):
        """Test that get_current_offset returning None logs error and returns True."""
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "calibration_received": True,
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.get_current_offset"
        ) as mock_get_offset:
            mock_convert.return_value = {
                "temperature": 20.0,
                "local_temperature_calibration": 2.0,
                "system_mode": HVACMode.HEAT,
            }
            # Fatal error: get_current_offset returns None
            mock_get_offset.return_value = None

            result = await control_trv(mock_self, "climate.trv1")

            # Should return True (no retry) on fatal error
            assert result is True

    @pytest.mark.asyncio
    async def test_call_for_heat_false_forces_off_mode(self):
        """Test that call_for_heat=False forces HVAC mode to OFF."""
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.window_open = False
        mock_self.call_for_heat = False  # No heat needed
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "last_hvac_mode": HVACMode.HEAT,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "system_mode_received": False,
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
        ) as mock_set_hvac, patch(
            "custom_components.better_thermostat.utils.controlling.override_set_hvac_mode"
        ) as mock_override:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False

            await control_trv(mock_self, "climate.trv1")

            # call_for_heat=False should force mode to OFF
            # set_hvac_mode should be called with OFF
            mock_set_hvac.assert_called_once()
            args = mock_set_hvac.call_args[0]
            assert args[2] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_check_system_mode_task_created(self):
        """Test that check_system_mode task is created when system_mode_received is True."""
        mock_state = Mock()
        mock_state.state = HVACMode.OFF  # Different from target
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        task_manager_mock = Mock()
        task_manager_mock.create_task = Mock()

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = task_manager_mock
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "last_hvac_mode": HVACMode.OFF,
                "system_mode_received": True,  # Should trigger task creation
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                },
                "target_temp_received": False,
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert, patch(
            "custom_components.better_thermostat.utils.controlling.set_hvac_mode"
        ) as mock_set_hvac, patch(
            "custom_components.better_thermostat.utils.controlling.override_set_hvac_mode"
        ) as mock_override:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False

            await control_trv(mock_self, "climate.trv1")

            # Task should be created for check_system_mode
            task_manager_mock.create_task.assert_called()

    @pytest.mark.asyncio
    async def test_lock_usage(self):
        """Test that _temp_lock is acquired during TRV control.

        The lock prevents race conditions when multiple TRVs are controlled
        in parallel by control_queue's asyncio.gather().
        """
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {"temperature": 20.0}

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        lock = asyncio.Lock()
        lock_acquire_mock = AsyncMock(wraps=lock.acquire)
        lock.acquire = lock_acquire_mock

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = lock
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        with patch(
            "custom_components.better_thermostat.utils.controlling.convert_outbound_states"
        ) as mock_convert:
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            # Lock should have been acquired
            lock_acquire_mock.assert_awaited()
