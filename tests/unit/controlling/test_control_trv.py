"""Tests for control_trv function in utils/controlling.py.

This is the most complex function in controlling.py with ~600 lines of code.
It has two main paths:
1. Unavailable TRV path (lines 263-591)
2. Available TRV path (lines 593-838)

Absorbed tests from:
- tests/unit/test_boost_mode.py (boost mode valve control & safety overrides)
- tests/unit/test_race_condition_lock_coverage.py (parallel TRV lock protection)
- tests/test_grouped_trv_calibration.py (calibration_received flag reset)
- tests/unit/test_unavailable_trv_no_operations.py (unavailable skip logic)
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import control_trv

# All delegate / helper functions that control_trv calls.  We patch them at the
# *controlling* module level because that is where they are imported.
_CTRL = "custom_components.better_thermostat.utils.controlling"
_PATCHES = {
    "convert_outbound_states": f"{_CTRL}.convert_outbound_states",
    "handle_window_open": f"{_CTRL}.handle_window_open",
    "set_hvac_mode": f"{_CTRL}.set_hvac_mode",
    "set_temperature": f"{_CTRL}.set_temperature",
    "set_offset": f"{_CTRL}.set_offset",
    "set_valve": f"{_CTRL}.set_valve",
    "get_current_offset": f"{_CTRL}.get_current_offset",
    "override_set_hvac_mode": f"{_CTRL}.override_set_hvac_mode",
}


def _close_coro(coro):
    """Close coroutine to avoid RuntimeWarning."""
    if inspect.iscoroutine(coro):
        coro.close()
    return Mock()


def _make_mock_self(trv_state=None, trv_attrs=None, real_trvs=None, **kwargs):
    """Create a mock BetterThermostat instance with common defaults.

    Parameters
    ----------
    trv_state : str or None
        The state to return from hass.states.get(). If None, returns None.
    trv_attrs : dict or None
        Attributes for the mock TRV state object.
    real_trvs : dict or None
        The real_trvs dict. If None, a minimal default is created.
    **kwargs : dict
        Additional attributes to set on mock_self (e.g. window_open, call_for_heat).
    """
    if trv_state is not None:
        mock_state = Mock()
        mock_state.state = trv_state
        mock_state.attributes = trv_attrs or {}
    else:
        mock_state = None

    mock_hass = Mock()
    mock_hass.states.get.return_value = mock_state
    mock_hass.services = Mock()
    mock_hass.services.async_call = AsyncMock()

    mock_self = Mock()
    mock_self.hass = mock_hass
    mock_self.device_name = "test_thermostat"
    mock_self._temp_lock = asyncio.Lock()
    mock_self.calculate_heating_power = AsyncMock()
    mock_self.bt_hvac_mode = kwargs.pop("bt_hvac_mode", HVACMode.HEAT)
    mock_self.window_open = kwargs.pop("window_open", False)
    mock_self.call_for_heat = kwargs.pop("call_for_heat", True)
    mock_self.cooler_entity_id = kwargs.pop("cooler_entity_id", None)
    mock_self.preset_mode = kwargs.pop("preset_mode", None)
    mock_self.cur_temp = kwargs.pop("cur_temp", 20.0)
    mock_self.bt_target_temp = kwargs.pop("bt_target_temp", 22.0)
    mock_self.context = kwargs.pop("context", None)
    mock_self.ignore_states = kwargs.pop("ignore_states", False)
    mock_self.task_manager = Mock(create_task=Mock())

    if real_trvs is None:
        real_trvs = {"climate.trv1": _default_trv_config()}
    mock_self.real_trvs = real_trvs

    # Set any additional attributes
    for key, value in kwargs.items():
        setattr(mock_self, key, value)

    return mock_self


def _default_trv_config(**overrides):
    """Return a default real_trvs entry for a single TRV."""
    cfg = {
        "ignore_trv_states": False,
        "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
        "min_temp": 5.0,
        "max_temp": 30.0,
        "temperature": 20.0,
        "last_temperature": 20.0,
        "last_hvac_mode": HVACMode.HEAT,
        "last_calibration": 0.0,
        "system_mode_received": False,
        "target_temp_received": False,
        "calibration_received": False,
        "hvac_mode": HVACMode.HEAT,
        "advanced": {
            "calibration_mode": CalibrationMode.NO_CALIBRATION,
            "calibration": CalibrationType.TARGET_TEMP_BASED,
            "no_off_system_mode": False,
        },
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Unavailable TRV path
# ---------------------------------------------------------------------------


class TestControlTrvUnavailablePath:
    """Test control_trv function when TRV is unavailable.

    When a TRV is unavailable, control_trv still calls convert_outbound_states
    and processes valve/temperature/mode changes, then sleeps 3s and returns True.
    """

    @pytest.mark.asyncio
    async def test_trv_none_returns_true(self):
        """Test that None TRV state enters unavailable path and returns True."""
        mock_self = _make_mock_self(trv_state=None)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            # Return None so the HVAC mode change condition short-circuits
            # (_new_hvac_mode is not None → False).  When _trv is None the
            # unavailable path cannot compare _trv.state without crashing.
            mock_window.return_value = None

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            assert mock_self.real_trvs["climate.trv1"]["ignore_trv_states"] is False

    @pytest.mark.asyncio
    async def test_trv_unavailable_returns_true(self):
        """Test that unavailable TRV returns True (no retry)."""
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_unavailable_trv_no_operations_called(self):
        """Unavailable TRV should return True immediately without calling any operations."""
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(_PATCHES["set_valve"]) as mock_set_valve,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            mock_convert.assert_not_called()
            mock_set_hvac.assert_not_called()
            mock_set_temp.assert_not_called()
            mock_set_valve.assert_not_called()

    @pytest.mark.asyncio
    async def test_trv_unknown_returns_true(self):
        """Test that unknown TRV returns True (no retry)."""
        mock_self = _make_mock_self(trv_state=STATE_UNKNOWN)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_convert_outbound_states_fails_returns_true(self):
        """Unavailable TRV with convert error should return True (no retry)."""
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = "ERROR"

            result = await control_trv(mock_self, "climate.trv1")

            # Expected: True (no retry for unavailable TRVs)
            assert result is True

    @pytest.mark.asyncio
    async def test_boost_mode_sets_max_temp_unavailable(self):
        """Test that boost mode sets temperature to max_temp for unavailable TRV.

        In the unavailable path, boost mode sets _temperature to max_temp (30).
        Note: the unavailable path also computes a valve bal dict for boost,
        but the set_valve call is inside the DIRECT_VALVE_BASED elif branch
        which is skipped because the boost if-branch was already taken.
        """
        mock_self = _make_mock_self(
            trv_state=STATE_UNAVAILABLE,
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            real_trvs={
                "climate.trv1": _default_trv_config(
                    advanced={
                        "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                        "calibration": CalibrationType.DIRECT_VALVE_BASED,
                        "no_off_system_mode": False,
                    }
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["get_current_offset"], new=AsyncMock(return_value=0.0)),
            patch(_PATCHES["set_offset"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "local_temperature_calibration": 0.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            # Boost sets temperature to max_temp (30.0)
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 30.0

    @pytest.mark.asyncio
    async def test_boost_mode_sets_max_temp(self):
        """Test that boost mode sets temperature to max_temp."""
        mock_self = _make_mock_self(
            trv_state=STATE_UNAVAILABLE,
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            real_trvs={"climate.trv1": _default_trv_config()},
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None
            mock_window.return_value = HVACMode.HEAT

            await control_trv(mock_self, "climate.trv1")

            # Should call set_temperature with max_temp (30.0)
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 30.0  # max_temp

    @pytest.mark.asyncio
    async def test_window_open_sets_mode_to_off(self):
        """Test that window open sets HVAC mode to OFF."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            window_open=True,
            real_trvs={"climate.trv1": _default_trv_config()},
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac,
            patch(_PATCHES["override_set_hvac_mode"]) as mock_override,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False
            # handle_window_open returns OFF when window is open
            mock_window.return_value = HVACMode.OFF

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            # set_hvac_mode should be called with OFF
            mock_set_hvac.assert_called_once()
            assert mock_set_hvac.call_args[0][2] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_no_off_mode_sends_min_temp_when_off_requested(self):
        """Test that TRV without OFF mode sends min_temp when OFF is requested."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            call_for_heat=False,  # No heat needed -> OFF
            real_trvs={
                "climate.trv1": _default_trv_config(
                    hvac_modes=[HVACMode.HEAT]  # No OFF mode!
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None
            mock_window.return_value = HVACMode.HEAT

            await control_trv(mock_self, "climate.trv1")

            # Should set temperature to min_temp (5.0) because OFF is not available
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 5.0  # min_temp

    @pytest.mark.asyncio
    async def test_ignore_trv_states_flag_set_and_reset(self):
        """Test that ignore_trv_states flag is set during processing and reset after."""
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_window.return_value = HVACMode.HEAT

            await control_trv(mock_self, "climate.trv1")

            # After completion, flag should be reset
            assert mock_self.real_trvs["climate.trv1"]["ignore_trv_states"] is False


# ---------------------------------------------------------------------------
# Available TRV path
# ---------------------------------------------------------------------------


class TestControlTrvAvailablePath:
    """Test control_trv function when TRV is available.

    This tests the available TRV path (after the unavailable check).
    """

    @pytest.mark.asyncio
    async def test_available_trv_normal_operation(self):
        """Test normal operation with available TRV."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_available_trv_convert_fails_returns_false(self):
        """Test that convert failure returns False for available TRV."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = "ERROR"

            result = await control_trv(mock_self, "climate.trv1")

            assert result is False

    @pytest.mark.asyncio
    async def test_boost_mode_sets_valve_in_available_path(self):
        """Boost mode should set valve to 100% for available TRVs with direct valve control."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            real_trvs={
                "climate.trv1": _default_trv_config(
                    advanced={
                        "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                        "calibration": CalibrationType.DIRECT_VALVE_BASED,
                        "no_off_system_mode": False,
                    }
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"]) as mock_set_valve,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_valve.return_value = True
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            mock_set_valve.assert_called_once()
            args = mock_set_valve.call_args[0]
            assert args[2] == 100

    @pytest.mark.asyncio
    async def test_grouped_trv_calibration_fix(self):
        """Test grouped TRV calibration fix.

        When get_current_offset matches the target calibration and
        calibration_received is False, it should be reset to True.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            real_trvs={
                "climate.trv1": _default_trv_config(
                    last_calibration=2.0,
                    calibration_received=False,  # Stuck at False
                    advanced={
                        "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                        "calibration": CalibrationType.TARGET_TEMP_BASED,
                        "no_off_system_mode": False,
                    },
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["get_current_offset"]) as mock_get_offset,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "local_temperature_calibration": 2.0,
                "system_mode": HVACMode.HEAT,
            }
            # Current calibration already matches target
            mock_get_offset.return_value = 2.0
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            # The fix should reset calibration_received to True
            assert mock_self.real_trvs["climate.trv1"]["calibration_received"] is True
            assert result is True

    @pytest.mark.asyncio
    async def test_get_current_offset_none_returns_true(self):
        """Test that get_current_offset returning None logs error and returns True."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            real_trvs={
                "climate.trv1": _default_trv_config(
                    calibration_received=True,
                    advanced={
                        "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                        "calibration": CalibrationType.TARGET_TEMP_BASED,
                        "no_off_system_mode": False,
                    },
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["get_current_offset"]) as mock_get_offset,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "local_temperature_calibration": 2.0,
                "system_mode": HVACMode.HEAT,
            }
            # Fatal error: get_current_offset returns None
            mock_get_offset.return_value = None
            mock_window.return_value = HVACMode.HEAT

            result = await control_trv(mock_self, "climate.trv1")

            # Should return True (no retry) on fatal error
            assert result is True

    @pytest.mark.asyncio
    async def test_call_for_heat_false_forces_off_mode(self):
        """Test that call_for_heat=False forces HVAC mode to OFF."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            call_for_heat=False,
            real_trvs={"climate.trv1": _default_trv_config()},
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac,
            patch(_PATCHES["override_set_hvac_mode"]) as mock_override,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False
            mock_window.return_value = HVACMode.HEAT

            await control_trv(mock_self, "climate.trv1")

            # call_for_heat=False should force mode to OFF
            mock_set_hvac.assert_called_once()
            args = mock_set_hvac.call_args[0]
            assert args[2] == HVACMode.OFF

    @pytest.mark.asyncio
    async def test_check_system_mode_task_created(self):
        """Test that check_system_mode task is created when system_mode_received is True."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.OFF,  # Different from target (HEAT)
            trv_attrs={"temperature": 20.0},
            real_trvs={
                "climate.trv1": _default_trv_config(
                    last_hvac_mode=HVACMode.OFF,
                    system_mode_received=True,  # Should trigger task creation
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac,
            patch(_PATCHES["override_set_hvac_mode"]) as mock_override,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False
            mock_window.return_value = HVACMode.HEAT

            await control_trv(mock_self, "climate.trv1")

            # Task should be created for check_system_mode
            mock_self.task_manager.create_task.assert_called()

    @pytest.mark.asyncio
    async def test_lock_usage(self):
        """Test that _temp_lock is acquired during TRV control.

        The lock prevents race conditions when multiple TRVs are controlled
        in parallel by control_queue's asyncio.gather().
        """
        lock = asyncio.Lock()
        lock_acquire_mock = AsyncMock(wraps=lock.acquire)
        lock.acquire = lock_acquire_mock

        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )
        mock_self._temp_lock = lock

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["handle_window_open"]) as mock_window,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_window.return_value = HVACMode.HEAT

            await control_trv(mock_self, "climate.trv1")

            # Lock should have been acquired
            lock_acquire_mock.assert_awaited()


# ---------------------------------------------------------------------------
# Boost mode with safety override (from test_boost_mode.py)
# ---------------------------------------------------------------------------


class TestBoostModeSafetyOverride:
    """Test safety override resets valve when HVAC is forced to OFF during boost mode.

    When boost mode sets valve to 100% but then HVAC is forced to OFF
    (window_open or call_for_heat=False), the valve must be reset to 0%
    to avoid a dangerous valve 100% + HVAC OFF conflict.
    """

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
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"], side_effect=track_set_valve),
            patch(_PATCHES["set_hvac_mode"]),
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
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"], side_effect=track_set_valve),
            patch(_PATCHES["set_hvac_mode"]),
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


# ---------------------------------------------------------------------------
# Race condition / lock coverage (from test_race_condition_lock_coverage.py)
# ---------------------------------------------------------------------------


class TestRaceConditionLockCoverage:
    """Test that parallel TRV control does not cause race conditions.

    The _temp_lock must protect all critical operations including
    set_valve(), set_hvac_mode(), set_offset(), and set_temperature()
    to prevent shared state corruption when multiple TRVs are controlled
    concurrently via asyncio.gather().
    """

    @pytest.mark.asyncio
    async def test_parallel_trv_control_no_race_condition(self):
        """Test that parallel control_trv() calls don't cause race conditions.

        Scenario: 2 grouped TRVs controlled simultaneously.
        Expected: Both TRVs complete successfully without state corruption.
        """
        mock_state_trv1 = Mock()
        mock_state_trv1.state = HVACMode.OFF
        mock_state_trv1.attributes = {
            "temperature": 18.0,
            "current_temperature": 20.0,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
        }

        mock_state_trv2 = Mock()
        mock_state_trv2.state = HVACMode.OFF
        mock_state_trv2.attributes = {
            "temperature": 18.0,
            "current_temperature": 20.0,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
        }

        mock_hass = Mock()
        mock_hass.states.get.side_effect = lambda entity_id: (
            mock_state_trv1 if entity_id == "climate.trv1" else mock_state_trv2
        )

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_grouped_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
        mock_self.cur_temp = 20.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "temperature": 18.0,
                "last_temperature": 18.0,
                "last_hvac_mode": HVACMode.OFF,
                "system_mode_received": True,
                "target_temp_received": True,
                "calibration_received": False,
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            },
            "climate.trv2": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "temperature": 18.0,
                "last_temperature": 18.0,
                "last_hvac_mode": HVACMode.OFF,
                "system_mode_received": True,
                "target_temp_received": True,
                "calibration_received": False,
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            },
        }

        execution_log = []
        lock_acquired_count = 0
        original_lock_acquire = mock_self._temp_lock.acquire

        async def tracked_acquire(*args, **kwargs):
            nonlocal lock_acquired_count
            lock_acquired_count += 1
            execution_log.append(f"lock_acquire_{lock_acquired_count}")
            result = await original_lock_acquire(*args, **kwargs)
            execution_log.append(f"lock_acquired_{lock_acquired_count}")
            return result

        mock_self._temp_lock.acquire = tracked_acquire

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"]) as mock_set_valve,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac_mode,
            patch(_PATCHES["set_offset"]) as mock_set_offset,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["get_current_offset"], new=AsyncMock(return_value=0.0)),
        ):
            mock_convert.return_value = {
                "temperature": 22.0,
                "local_temperature_calibration": 0.0,
                "system_mode": HVACMode.HEAT,
            }

            async def delayed_set_valve(*args, **kwargs):
                execution_log.append(f"set_valve_start_{args[1]}")
                await asyncio.sleep(0.01)
                execution_log.append(f"set_valve_end_{args[1]}")

            async def delayed_set_hvac_mode(*args, **kwargs):
                execution_log.append(f"set_hvac_mode_start_{args[1]}")
                await asyncio.sleep(0.01)
                execution_log.append(f"set_hvac_mode_end_{args[1]}")

            async def delayed_set_offset(*args, **kwargs):
                execution_log.append(f"set_offset_start_{args[1]}")
                await asyncio.sleep(0.01)
                execution_log.append(f"set_offset_end_{args[1]}")

            async def delayed_set_temp(*args, **kwargs):
                execution_log.append(f"set_temp_start_{args[1]}")
                await asyncio.sleep(0.01)
                execution_log.append(f"set_temp_end_{args[1]}")

            mock_set_valve.side_effect = delayed_set_valve
            mock_set_hvac_mode.side_effect = delayed_set_hvac_mode
            mock_set_offset.side_effect = delayed_set_offset
            mock_set_temp.side_effect = delayed_set_temp

            results = await asyncio.gather(
                control_trv(mock_self, "climate.trv1"),
                control_trv(mock_self, "climate.trv2"),
                return_exceptions=True,
            )

            assert results[0] is True
            assert results[1] is True

            total_calls = (
                mock_set_temp.call_count
                + mock_set_hvac_mode.call_count
                + mock_set_offset.call_count
                + mock_set_valve.call_count
            )
            assert total_calls >= 2, (
                f"Expected at least 2 operation calls, got {total_calls}"
            )

            # Check for interleaving of operations
            operation_events = [
                e for e in execution_log if "set_hvac_mode" in e or "set_temp" in e
            ]

            if len(operation_events) >= 4:
                starts = [e for e in operation_events if "start" in e]
                ends = [e for e in operation_events if "end" in e]

                if len(starts) >= 2 and len(ends) >= 2:
                    for i, event in enumerate(operation_events):
                        if "end" in event:
                            starts_before = sum(
                                1 for e in operation_events[:i] if "start" in e
                            )
                            ends_before = sum(
                                1 for e in operation_events[:i] if "end" in e
                            )
                            if starts_before > ends_before + 1:
                                raise AssertionError(
                                    f"Race condition detected: {starts_before} "
                                    f"operations started before this one ended.\n"
                                    f"  Event: {event}\n"
                                    f"  Events before: {operation_events[:i]}"
                                )

    @pytest.mark.asyncio
    async def test_shared_state_corruption_in_parallel_execution(self):
        """Test that shared state doesn't get corrupted during parallel execution."""
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {
            "temperature": 22.0,
            "current_temperature": 20.0,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
        }

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
        mock_self.cur_temp = 20.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True

        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "temperature": 22.0,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            },
            "climate.trv2": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "temperature": 22.0,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            },
        }

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"]),
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["get_current_offset"], new=AsyncMock(return_value=0.0)),
        ):
            mock_convert.return_value = {
                "temperature": 22.0,
                "local_temperature_calibration": 0.0,
                "system_mode": HVACMode.HEAT,
            }

            results = await asyncio.gather(
                control_trv(mock_self, "climate.trv1"),
                control_trv(mock_self, "climate.trv2"),
                return_exceptions=True,
            )

            assert results[0] is True
            assert results[1] is True
            assert mock_self.real_trvs["climate.trv1"]["ignore_trv_states"] is False
            assert mock_self.real_trvs["climate.trv2"]["ignore_trv_states"] is False

    @pytest.mark.asyncio
    async def test_lock_protects_critical_sections(self):
        """Test that lock actually protects all critical operations."""
        mock_state = Mock()
        mock_state.state = HVACMode.HEAT
        mock_state.attributes = {
            "temperature": 22.0,
            "current_temperature": 20.0,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
        }

        mock_hass = Mock()
        mock_hass.states.get.return_value = mock_state

        mock_self = Mock()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
        mock_self.cur_temp = 20.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.real_trvs = {
            "climate.trv1": {
                "ignore_trv_states": False,
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "temperature": 22.0,
                "last_hvac_mode": HVACMode.HEAT,
                "system_mode_received": False,
                "target_temp_received": False,
                "calibration_received": False,
                "model_quirks": Mock(
                    override_set_hvac_mode=AsyncMock(return_value=False)
                ),
                "advanced": {
                    "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                },
            }
        }

        lock_state_during_operations = []

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"]) as mock_set_valve,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac_mode,
            patch(_PATCHES["set_offset"]) as mock_set_offset,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["get_current_offset"], new=AsyncMock(return_value=0.0)),
        ):
            mock_convert.return_value = {
                "temperature": 22.0,
                "local_temperature_calibration": 0.0,
                "system_mode": HVACMode.HEAT,
            }

            async def check_lock_on_set_valve(*args, **kwargs):
                lock_state_during_operations.append(
                    ("set_valve", mock_self._temp_lock.locked())
                )

            async def check_lock_on_set_hvac_mode(*args, **kwargs):
                lock_state_during_operations.append(
                    ("set_hvac_mode", mock_self._temp_lock.locked())
                )

            async def check_lock_on_set_offset(*args, **kwargs):
                lock_state_during_operations.append(
                    ("set_offset", mock_self._temp_lock.locked())
                )

            async def check_lock_on_set_temp(*args, **kwargs):
                lock_state_during_operations.append(
                    ("set_temperature", mock_self._temp_lock.locked())
                )

            mock_set_valve.side_effect = check_lock_on_set_valve
            mock_set_hvac_mode.side_effect = check_lock_on_set_hvac_mode
            mock_set_offset.side_effect = check_lock_on_set_offset
            mock_set_temp.side_effect = check_lock_on_set_temp

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

            for operation, locked in lock_state_during_operations:
                assert locked is True, (
                    f"Operation {operation} ran WITHOUT lock protection! "
                    f"This causes race conditions in parallel execution."
                )


# ---------------------------------------------------------------------------
# Grouped TRV calibration (from test_grouped_trv_calibration.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt_grouped():
    """Create a mock BetterThermostat instance for grouped TRV testing."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = "heat"
    bt.bt_target_temp = 21.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.call_for_heat = True
    bt.tolerance = 0.5
    bt._temp_lock = asyncio.Lock()
    bt.calculate_heating_power = AsyncMock()

    bt.real_trvs = {
        "climate.trv_1": {
            "calibration_received": True,
            "last_calibration": 2.0,
            "current_temperature": 20.0,
            "hvac_modes": ["heat", "off"],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "ignore_trv_states": False,
            "advanced": {
                "calibration": 0,  # LOCAL_BASED
                "calibration_mode": 0,  # DEFAULT
            },
        },
        "climate.trv_2": {
            "calibration_received": True,
            "last_calibration": 2.0,
            "current_temperature": 20.0,
            "hvac_modes": ["heat", "off"],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "ignore_trv_states": False,
            "advanced": {"calibration": 0, "calibration_mode": 0},
        },
        "climate.trv_3": {
            "calibration_received": False,  # Stuck at False!
            "last_calibration": 2.0,
            "current_temperature": 20.0,
            "hvac_modes": ["heat", "off"],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "ignore_trv_states": False,
            "advanced": {"calibration": 0, "calibration_mode": 0},
        },
    }
    return bt


class TestGroupedTrvCalibration:
    """Tests for calibration_received flag reset with grouped TRVs.

    Issue #1410: When controlling multiple TRVs as a group with offset calibration,
    not all TRVs receive updated calibration simultaneously. The calibration_received
    flag can get stuck at False, blocking future calibration updates.
    """

    @pytest.mark.anyio
    async def test_calibration_received_stays_false_when_mismatch(
        self, mock_bt_grouped
    ):
        """Test that calibration_received stays False when calibration differs."""
        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], new_callable=AsyncMock
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], new_callable=AsyncMock) as mock_set_offset,
            patch(_PATCHES["set_temperature"], new_callable=AsyncMock),
            patch(_PATCHES["set_hvac_mode"], new_callable=AsyncMock),
            patch(_PATCHES["set_valve"], new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_get_offset.return_value = 2.0
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 3.0,  # Different from current!
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is False

            await control_trv(mock_bt_grouped, entity_id)

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is False
            mock_set_offset.assert_not_called()

    @pytest.mark.anyio
    async def test_calibration_sent_when_received_true_and_differs(
        self, mock_bt_grouped
    ):
        """Test that calibration is sent when flag is True and values differ."""
        entity_id = "climate.trv_1"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], new_callable=AsyncMock
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], new_callable=AsyncMock) as mock_set_offset,
            patch(_PATCHES["set_temperature"], new_callable=AsyncMock),
            patch(_PATCHES["set_hvac_mode"], new_callable=AsyncMock),
            patch(_PATCHES["set_valve"], new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_get_offset.return_value = 2.0
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 3.0,  # Different!
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is True

            await control_trv(mock_bt_grouped, entity_id)

            mock_set_offset.assert_called_once_with(mock_bt_grouped, entity_id, 3.0)
            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is False

    @pytest.mark.anyio
    async def test_calibration_tolerance_within_half_degree(self, mock_bt_grouped):
        """Test that calibration within 0.5 degree tolerance is considered matching."""
        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], new_callable=AsyncMock
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], new_callable=AsyncMock),
            patch(_PATCHES["set_temperature"], new_callable=AsyncMock),
            patch(_PATCHES["set_hvac_mode"], new_callable=AsyncMock),
            patch(_PATCHES["set_valve"], new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_get_offset.return_value = 2.3
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 2.0,
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is False

            await control_trv(mock_bt_grouped, entity_id)

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is True

    @pytest.mark.anyio
    async def test_calibration_tolerance_outside_half_degree(self, mock_bt_grouped):
        """Test that calibration outside 0.5 degree tolerance is not matching."""
        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], new_callable=AsyncMock
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], new_callable=AsyncMock),
            patch(_PATCHES["set_temperature"], new_callable=AsyncMock),
            patch(_PATCHES["set_hvac_mode"], new_callable=AsyncMock),
            patch(_PATCHES["set_valve"], new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_get_offset.return_value = 2.6
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 2.0,
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is False

            await control_trv(mock_bt_grouped, entity_id)

            assert mock_bt_grouped.real_trvs[entity_id]["calibration_received"] is False
