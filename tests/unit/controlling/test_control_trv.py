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
from dataclasses import replace
import inspect
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.reachability import ReachabilityState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.snapshot import (
    parse_hvac_mode as _parse_mode,
)
from custom_components.better_thermostat.trv import Trv
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
    "set_hvac_mode": f"{_CTRL}.set_hvac_mode",
    "set_temperature": f"{_CTRL}.set_temperature",
    "set_offset": f"{_CTRL}.set_offset",
    "set_valve": f"{_CTRL}.set_valve",
    "get_current_offset": f"{_CTRL}.get_current_offset",
    "override_set_hvac_mode": f"{_CTRL}.override_set_hvac_mode",
    "override_set_temperature": f"{_CTRL}.override_set_temperature",
}


def _close_coro(coro, **kwargs):
    """Close coroutine to avoid RuntimeWarning."""
    if inspect.iscoroutine(coro):
        coro.close()
    return Mock()


def _kernel_state_for(mock_self):
    """Kernel regions mirroring the mock's flag attributes (like production)."""
    state = running_kernel_state()
    parsed = _parse_mode(str(mock_self.bt_hvac_mode))
    if parsed is not None:
        state = replace(state, mode=ModeState(hvac_mode=parsed))
    if mock_self.window_open:
        state = replace(state, window=WindowState(phase=WindowPhase.OPEN))
    return state


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
    mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
    mock_self.clock = FakeClock()
    mock_self.startup_running = False
    mock_self.in_maintenance = False
    mock_self.degraded_mode = False
    mock_self.outdoor_sensor = None
    mock_self.weather_entity = None
    mock_self.cur_temp_filtered = None
    mock_self.temp_slope = None
    mock_self.bt_target_cooltemp = None
    mock_self.tolerance = kwargs.pop("tolerance", 0.0)
    mock_self.bt_min_temp = 5.0
    mock_self.bt_max_temp = 30.0

    if real_trvs is None:
        real_trvs = {"climate.trv1": _default_trv_config()}
    mock_self.real_trvs = real_trvs

    # Set any additional attributes
    for key, value in kwargs.items():
        setattr(mock_self, key, value)

    mock_self.kernel_state = _kernel_state_for(mock_self)

    return mock_self


def _default_trv_config(**overrides):
    """Return a default real_trvs entry (a Trv) for a single TRV."""
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
    return Trv.from_legacy_dict("climate.trv1", cfg)


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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is False

    @pytest.mark.asyncio
    async def test_trv_unavailable_returns_true(self):
        """Test that unavailable TRV returns True (no retry)."""
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_offline_trv_schedules_reachability_retry(self):
        """Skipping an offline TRV schedules the region's retry.

        Consumes the reachability region's retry_at: a follow-up
        control cycle is scheduled for the retry window.
        """
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)
        mock_self.kernel_state = replace(
            mock_self.kernel_state,
            reachability={
                "climate.trv1": ReachabilityState(
                    online=False, offline_since=100.0, retry_count=0, retry_at=130.0
                )
            },
        )
        mock_self.real_trvs["climate.trv1"].reachability_retry_pending = False

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
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

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            assert (
                mock_self.real_trvs["climate.trv1"].reachability_retry_pending is True
            )
            assert mock_self.task_manager.create_task.called

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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True
            # Boost sets temperature to max_temp (30.0)
            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 30.0

    @pytest.mark.asyncio
    async def test_boost_mode_sets_max_temp(self):
        """Boost on a DIRECT_VALVE_BASED TRV sets temperature to max_temp."""
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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["get_current_offset"], new=AsyncMock(return_value=0.0)),
            patch(_PATCHES["set_offset"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
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
    async def test_boost_mode_offset_does_not_override_temp(self):
        """Boost on an offset-mode TRV keeps the calibrated setpoint, not max."""
        mock_self = _make_mock_self(
            trv_state=STATE_UNAVAILABLE,
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            real_trvs={
                "climate.trv1": _default_trv_config(
                    advanced={
                        "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                        "calibration": CalibrationType.LOCAL_BASED,
                        "no_off_system_mode": False,
                    }
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch(_PATCHES["set_valve"]) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["get_current_offset"], new=AsyncMock(return_value=0.0)),
            patch(_PATCHES["set_offset"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 22.0,
                "local_temperature_calibration": -1.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            await control_trv(mock_self, "climate.trv1")

            mock_set_temp.assert_called_once()
            args = mock_set_temp.call_args[0]
            assert args[2] == 22.0  # calibrated setpoint, not max_temp
            mock_set_valve.assert_not_called()

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
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False

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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
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
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            # After completion, flag should be reset
            assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is False


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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_set_temperature_quirk_skips_generic_adapter(self):
        """A model quirk that handles the write suppresses the adapter call."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=True)
            ) as mock_override,
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            mock_override.assert_awaited_once_with(mock_self, "climate.trv1", 21.0)
            mock_set_temp.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_guard_matches_across_float_rounding_grids(self):
        """An already-applied setpoint is not re-sent despite grid mismatch.

        The outbound value comes from the device step grid
        (round_by_step(20.7, 0.1) == 20.700000000000003), while the TRV
        state reads back 20.7 through the 0.01 grid. The tolerance-based
        skip guard must recognize the match and suppress the write.
        """
        from custom_components.better_thermostat.utils.helpers import round_by_step

        outbound = round_by_step(20.7, 0.1)
        assert outbound != 20.7  # the grids genuinely diverge

        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.7}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ) as mock_override,
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": outbound,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            mock_override.assert_not_called()
            mock_set_temp.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_temperature_falls_back_to_generic_adapter(self):
        """Without a model quirk, the generic adapter performs the write."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ) as mock_override,
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"]) as mock_set_temp,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            mock_override.assert_awaited_once_with(mock_self, "climate.trv1", 21.0)
            mock_set_temp.assert_awaited_once_with(mock_self, "climate.trv1", 21.0)

    @pytest.mark.asyncio
    async def test_available_trv_convert_fails_returns_false(self):
        """Test that convert failure returns False for available TRV.

        The failing worker must not back off under the TRV lock: every
        other TRV of the cycle contends for it, so a sleep taken here
        stalls the whole cycle on the one device that failed.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        lock_held_during_sleep = []

        async def record_lock_state(*args, **kwargs):
            lock_held_during_sleep.append(mock_self._temp_lock.locked())

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch("asyncio.sleep", new=AsyncMock(side_effect=record_lock_state)),
        ):
            mock_convert.return_value = "ERROR"

            result = await control_trv(mock_self, "climate.trv1")

            assert result is False
            # No sleep on this path ran while holding the lock.
            assert not any(lock_held_during_sleep)

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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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

            result = await control_trv(mock_self, "climate.trv1")

            # The fix should reset calibration_received to True
            assert mock_self.real_trvs["climate.trv1"].calibration_received is True
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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False

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
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_hvac.return_value = None
            mock_override.return_value = False

            await control_trv(mock_self, "climate.trv1")

            # Task should be created for check_system_mode
            mock_self.task_manager.create_task.assert_called()

    @pytest.mark.asyncio
    async def test_dropout_after_valve_write_sends_no_hvac_mode(self):
        """A TRV that drops offline during the cycle gets no mode write.

        The mode is re-read once the valve write has awaited. An
        ``unavailable`` reading is not a reported mode: taken as one it
        makes the unchanged intent look like a change, so BT addresses a
        device that cannot answer and books the write as settled.
        """
        offline_state = Mock()
        offline_state.state = STATE_UNAVAILABLE
        offline_state.attributes = {}

        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            real_trvs={
                "climate.trv1": _default_trv_config(
                    system_mode_received=True,
                    advanced={
                        "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                        "calibration": CalibrationType.DIRECT_VALVE_BASED,
                        "no_off_system_mode": False,
                    },
                )
            },
        )
        # A valve balance gives the cycle a write to await on.
        mock_self.real_trvs["climate.trv1"].calibration_balance = {
            "apply_valve": True,
            "valve_percent": 80,
        }

        live_state = mock_self.hass.states.get("climate.trv1")
        mock_self.hass.states.get = Mock(side_effect=lambda *a, **k: live_state)

        async def drop_trv_offline(*args, **kwargs):
            nonlocal live_state
            live_state = offline_state
            return True

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_valve"], side_effect=drop_trv_offline
            ) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ) as mock_override_hvac,
            patch(_PATCHES["set_hvac_mode"]) as mock_set_hvac,
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

        assert result is True
        mock_set_valve.assert_awaited_once()
        # The intent still matches the last mode the TRV reported.
        mock_override_hvac.assert_not_called()
        mock_set_hvac.assert_not_called()
        assert mock_self.real_trvs["climate.trv1"].system_mode_received is True

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
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            # Lock should have been acquired
            lock_acquire_mock.assert_awaited()


class TestControlTrvIgnoreFlagReset:
    """The ignore_trv_states flag never survives control_trv.

    While the flag is True, TRV-side user setpoint changes are dropped, so
    every exit path (return, exception, cancellation) must reset it.
    """

    @pytest.mark.asyncio
    async def test_adapter_exception_resets_ignore_trv_states(self):
        """A failing adapter write propagates but still resets the flag."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(
                _PATCHES["set_temperature"],
                new=AsyncMock(side_effect=RuntimeError("adapter failure")),
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            with pytest.raises(RuntimeError, match="adapter failure"):
                await control_trv(mock_self, "climate.trv1")

        assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is False

    @pytest.mark.asyncio
    async def test_cancellation_resets_ignore_trv_states(self):
        """Cancelling control_trv mid-write resets the flag."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )
        entered_write = asyncio.Event()
        release_write = asyncio.Event()

        async def _blocking_set_temperature(*args, **kwargs):
            entered_write.set()
            await release_write.wait()

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=_blocking_set_temperature),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            task = asyncio.create_task(control_trv(mock_self, "climate.trv1"))
            await asyncio.wait_for(entered_write.wait(), timeout=5)
            assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is True

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is False

    @pytest.mark.asyncio
    async def test_cancel_while_waiting_for_lock_keeps_holder_flag(self):
        """Cancelling a caller queued on the lock leaves the holder's flag alone.

        Only the invocation that set ignore_trv_states may clear it. A second
        invocation cancelled while still waiting for _temp_lock never set the
        flag, so its cleanup must not clear it for the concurrent holder that
        is mid-write.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )
        entered_write = asyncio.Event()
        release_write = asyncio.Event()

        async def _blocking_set_temperature(*args, **kwargs):
            entered_write.set()
            await release_write.wait()

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch(_PATCHES["set_temperature"], new=_blocking_set_temperature),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            holder = asyncio.create_task(control_trv(mock_self, "climate.trv1"))
            await asyncio.wait_for(entered_write.wait(), timeout=5)
            assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is True

            waiter = asyncio.create_task(control_trv(mock_self, "climate.trv1"))
            # Let the waiter run until it suspends on the held lock.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter

            # The holder is still mid-write; its suppression flag must survive.
            assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is True

            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder

        assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is False


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
        mock_self.clock = FakeClock()
        mock_self.startup_running = False
        mock_self.in_maintenance = False
        mock_self.degraded_mode = False
        mock_self.ignore_states = False
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
        mock_self.cur_temp_filtered = None
        mock_self.temp_slope = None
        mock_self.bt_target_cooltemp = None
        mock_self.tolerance = 0.0
        mock_self.bt_min_temp = 5.0
        mock_self.bt_max_temp = 30.0

        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
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
                },
            )
        }

        mock_self.kernel_state = _kernel_state_for(mock_self)

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
    async def test_boost_does_not_override_hold(self):
        """The HOLD rung outranks boost.

        During a total sensor outage no valve write happens, boost
        preset or not. The setpoint channel locks the raw user target
        (passthrough through the safety hull) instead.
        """
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
        mock_self.kernel_state = replace(
            mock_self.kernel_state, control_mode=ControlModeState(mode=ControlMode.HOLD)
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"], new=AsyncMock()) as mock_set_temp,
            patch(_PATCHES["set_valve"], new=AsyncMock()) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            result = await control_trv(mock_self, "climate.trv1")

        assert result is True
        mock_set_valve.assert_not_called()
        # The raw target is locked on the device (22.0, not boost max).
        mock_set_temp.assert_called_once()
        assert mock_set_temp.call_args[0][2] == 22.0

    @pytest.mark.asyncio
    async def test_boost_safety_reset_stamps_the_valve_budget(self):
        """The 0% safety reset occupies the valve budget slot.

        It bypasses the budget gate (closing is the safe direction) but
        stamps the slot like every other valve write.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            window_open=True,
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
        # A valve write 10 s ago keeps the budget closed for the boost
        # 100% write; only the safety reset may run.
        mock_self.real_trvs["climate.trv1"].last_valve_write_monotonic = 0.0
        mock_self.clock.advance(10.0)

        set_valve_calls = []

        async def track_set_valve(*args, **kwargs):
            set_valve_calls.append(args)
            return True

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"], side_effect=track_set_valve),
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
            result = await control_trv(mock_self, "climate.trv1")

        assert result is True
        assert [call[2] for call in set_valve_calls] == [0]
        assert mock_self.real_trvs["climate.trv1"].last_valve_write_monotonic == 10.0

    @pytest.mark.asyncio
    async def test_failed_safety_reset_schedules_a_retry_cycle(self):
        """A failed 0% safety reset is re-requested like any other write.

        The budget slot is already stamped when the delegate reports
        failure, so without a follow-up cycle the valve stays at 100%
        with HVAC OFF until some unrelated event triggers control.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            window_open=True,
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

        captured = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: captured.append((coro, name)) or Mock()
        )

        set_valve_calls = []

        async def failing_set_valve(*args, **kwargs):
            set_valve_calls.append(args)
            # The boost 100% write succeeds; the 0% safety reset fails.
            return args[2] != 0

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"], side_effect=failing_set_valve),
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
            result = await control_trv(mock_self, "climate.trv1")

        assert result is True
        assert [call[2] for call in set_valve_calls] == [100, 0]

        retries = [
            (coro, name) for coro, name in captured if "budget_retry" in (name or "")
        ]
        assert len(retries) == 1
        assert mock_self.real_trvs["climate.trv1"].budget_retry_pending is True

        coro, _name = retries[0]
        with patch("asyncio.sleep", new=AsyncMock()):
            await coro
        mock_self.control_queue_task.put_nowait.assert_called_once()
        assert mock_self.real_trvs["climate.trv1"].budget_retry_pending is False

        # Close any other captured coroutines to avoid RuntimeWarning.
        for coro, name in captured:
            if "budget_retry" not in (name or ""):
                coro.close()

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
        mock_self.clock = FakeClock()
        mock_self.startup_running = False
        mock_self.in_maintenance = False
        mock_self.degraded_mode = False
        mock_self.ignore_states = False
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
        mock_self.cur_temp_filtered = None
        mock_self.temp_slope = None
        mock_self.bt_target_cooltemp = None
        mock_self.tolerance = 0.0
        mock_self.bt_min_temp = 5.0
        mock_self.bt_max_temp = 30.0

        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
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
                },
            )
        }

        mock_self.kernel_state = _kernel_state_for(mock_self)

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
        mock_self.clock = FakeClock()
        mock_self.startup_running = False
        mock_self.in_maintenance = False
        mock_self.degraded_mode = False
        mock_self.ignore_states = False
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
        mock_self.cur_temp_filtered = None
        mock_self.temp_slope = None
        mock_self.bt_target_cooltemp = None
        mock_self.tolerance = 0.0
        mock_self.bt_min_temp = 5.0
        mock_self.bt_max_temp = 30.0
        mock_self.preset_mode = None
        mock_self.cooler_entity_id = None
        mock_self.cur_temp = 20.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True

        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
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
            ),
            "climate.trv2": Trv.from_legacy_dict(
                "climate.trv2",
                {
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
            ),
        }

        mock_self.kernel_state = _kernel_state_for(mock_self)

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
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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
        mock_self.clock = FakeClock()
        mock_self.startup_running = False
        mock_self.in_maintenance = False
        mock_self.degraded_mode = False
        mock_self.ignore_states = False
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
        mock_self.cur_temp_filtered = None
        mock_self.temp_slope = None
        mock_self.bt_target_cooltemp = None
        mock_self.tolerance = 0.0
        mock_self.bt_min_temp = 5.0
        mock_self.bt_max_temp = 30.0
        mock_self.preset_mode = None
        mock_self.cooler_entity_id = None
        mock_self.cur_temp = 20.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True

        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
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
            ),
            "climate.trv2": Trv.from_legacy_dict(
                "climate.trv2",
                {
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
            ),
        }

        mock_self.kernel_state = _kernel_state_for(mock_self)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"]),
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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
            assert mock_self.real_trvs["climate.trv1"].ignore_trv_states is False
            assert mock_self.real_trvs["climate.trv2"].ignore_trv_states is False

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
        mock_self.clock = FakeClock()
        mock_self.startup_running = False
        mock_self.in_maintenance = False
        mock_self.degraded_mode = False
        mock_self.ignore_states = False
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None
        mock_self.cur_temp_filtered = None
        mock_self.temp_slope = None
        mock_self.bt_target_cooltemp = None
        mock_self.tolerance = 0.0
        mock_self.bt_min_temp = 5.0
        mock_self.bt_max_temp = 30.0
        mock_self.preset_mode = None
        mock_self.cooler_entity_id = None
        mock_self.cur_temp = 20.0
        mock_self.bt_target_temp = 22.0
        mock_self.bt_hvac_mode = HVACMode.HEAT
        mock_self.window_open = False
        mock_self.call_for_heat = True
        mock_self.real_trvs = {
            "climate.trv1": Trv.from_legacy_dict(
                "climate.trv1",
                {
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
            )
        }

        mock_self.kernel_state = _kernel_state_for(mock_self)

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
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
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

    @pytest.mark.asyncio
    async def test_deferred_setpoint_settles_outside_the_lock(self):
        """A budget-deferred setpoint must not hold the TRV lock while settling.

        Every TRV of a cycle contends for the same _temp_lock, so a
        settle sleep taken inside it serialises the whole cycle on the
        slowest deferral instead of overlapping them.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 18.0},
            cur_temp=18.0,
            bt_target_temp=22.0,
        )
        # A setpoint write 10 s ago keeps the budget closed, so the
        # differing target below is deferred rather than written.
        mock_self.real_trvs["climate.trv1"].last_write_monotonic = 0.0
        mock_self.clock.advance(10.0)

        lock_held_during_sleep = []

        async def record_lock_state(*args, **kwargs):
            lock_held_during_sleep.append(mock_self._temp_lock.locked())

        set_temperature_calls = []

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["set_temperature"],
                new=AsyncMock(
                    side_effect=lambda *a, **k: set_temperature_calls.append(a)
                ),
            ),
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
            patch("asyncio.sleep", new=AsyncMock(side_effect=record_lock_state)),
        ):
            mock_convert.return_value = {
                "temperature": 22.0,
                "system_mode": HVACMode.HEAT,
            }
            result = await control_trv(mock_self, "climate.trv1")

        assert result is True
        # The write was deferred, not sent.
        assert set_temperature_calls == []
        # The settle sleep ran, and never while holding the lock.
        assert lock_held_during_sleep
        assert not any(lock_held_during_sleep)
        assert mock_self._temp_lock.locked() is False


# ---------------------------------------------------------------------------
# Grouped TRV calibration (from test_grouped_trv_calibration.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt_grouped():
    """Create a mock BetterThermostat instance for grouped TRV testing."""
    bt = MagicMock()
    bt.hass = MagicMock()
    bt.clock = FakeClock()
    bt.startup_running = False
    bt.in_maintenance = False
    bt.degraded_mode = False
    bt.ignore_states = False
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.cur_temp_filtered = None
    bt.temp_slope = None
    bt.bt_target_cooltemp = None
    bt.tolerance = 0.0
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.device_name = "Test Thermostat"
    bt.bt_hvac_mode = "heat"
    bt.bt_target_temp = 21.0
    bt.cur_temp = 20.0
    bt.window_open = False
    bt.call_for_heat = True
    bt.tolerance = 0.5
    bt._temp_lock = asyncio.Lock()
    bt.calculate_heating_power = AsyncMock()

    bt.kernel_state = _kernel_state_for(bt)
    bt.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
    bt.real_trvs = {
        "climate.trv_1": Trv.from_legacy_dict(
            "climate.trv_1",
            {
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
        ),
        "climate.trv_2": Trv.from_legacy_dict(
            "climate.trv_2",
            {
                "calibration_received": True,
                "last_calibration": 2.0,
                "current_temperature": 20.0,
                "hvac_modes": ["heat", "off"],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "ignore_trv_states": False,
                "advanced": {"calibration": 0, "calibration_mode": 0},
            },
        ),
        "climate.trv_3": Trv.from_legacy_dict(
            "climate.trv_3",
            {
                "calibration_received": False,  # Stuck at False!
                "last_calibration": 2.0,
                "current_temperature": 20.0,
                "hvac_modes": ["heat", "off"],
                "min_temp": 5.0,
                "max_temp": 30.0,
                "ignore_trv_states": False,
                "advanced": {"calibration": 0, "calibration_mode": 0},
            },
        ),
    }
    return bt


class TestGroupedTrvCalibration:
    """Tests for calibration_received flag reset with grouped TRVs.

    Issue #1410: When controlling multiple TRVs as a group with offset calibration,
    not all TRVs receive updated calibration simultaneously. The calibration_received
    flag can get stuck at False, blocking future calibration updates.
    """

    @pytest.mark.anyio
    async def test_confirmed_command_releases_the_gate_and_writes_the_new_intent(
        self, mock_bt_grouped
    ):
        """A device holding its last command accepts a changed intent.

        The report equals the value last written, so the unacknowledged
        write is confirmed; the gate opens and the new intent goes out
        instead of the channel stalling on a flag nobody clears.
        """
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
            mock_get_offset.return_value = 2.0  # confirms last_calibration
            mock_set_offset.return_value = True
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 3.0,  # the intent moved
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is False

            await control_trv(mock_bt_grouped, entity_id)

            mock_set_offset.assert_awaited_once_with(mock_bt_grouped, entity_id, 3.0)
            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is False

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

            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is True

            await control_trv(mock_bt_grouped, entity_id)

            mock_set_offset.assert_called_once_with(mock_bt_grouped, entity_id, 3.0)
            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is False

    @pytest.mark.anyio
    async def test_missing_reference_calibration_skips_offset_write(
        self, mock_bt_grouped
    ):
        """No reference calibration skips the offset write without aborting.

        With no stored last_calibration and an unparseable device offset
        there is nothing to compare against; the cycle still performs the
        setpoint write instead of failing.
        """
        entity_id = "climate.trv_1"
        mock_bt_grouped.real_trvs[entity_id].last_calibration = None

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 20.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], new_callable=AsyncMock
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], new_callable=AsyncMock) as mock_set_offset,
            patch(_PATCHES["set_temperature"], new_callable=AsyncMock) as mock_set_temp,
            patch(_PATCHES["set_hvac_mode"], new_callable=AsyncMock),
            patch(_PATCHES["set_valve"], new_callable=AsyncMock),
            patch(
                _PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)
            ),
            patch(
                _PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_get_offset.return_value = "not-a-number"
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 3.0,
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            result = await control_trv(mock_bt_grouped, entity_id)

            assert result is True
            mock_set_offset.assert_not_called()
            mock_set_temp.assert_called_once()

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("step", "reported", "released"),
        [(0.5, 2.2, True), (0.5, 2.3, False), (1.0, 2.5, True)],
    )
    async def test_confirmation_window_is_half_the_device_step(
        self, mock_bt_grouped, step, reported, released
    ):
        """A report within half the device's own offset step confirms.

        That is the distance a device can move a written value by
        snapping it onto its own grid, so it is the width of the window
        in which the report still counts as the command.
        """
        entity_id = "climate.trv_3"
        mock_bt_grouped.real_trvs[entity_id].local_calibration_step = step

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
            mock_get_offset.return_value = reported
            mock_convert.return_value = {
                "temperature": 21.0,
                "local_temperature_calibration": 2.0,
                "local_temperature": 20.0,
                "system_mode": "heat",
            }

            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is False

            await control_trv(mock_bt_grouped, entity_id)

            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is released

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

            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is False

            await control_trv(mock_bt_grouped, entity_id)

            assert mock_bt_grouped.real_trvs[entity_id].calibration_received is False


# ---------------------------------------------------------------------------
# Offset write gate: intent, command and the device's report
# ---------------------------------------------------------------------------


def _offset_trv_config(**overrides):
    """Return a Trv configured for offset (LOCAL_BASED) calibration."""
    cfg = {
        "ignore_trv_states": False,
        "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
        "min_temp": 5.0,
        "max_temp": 30.0,
        "temperature": 20.0,
        "last_temperature": 20.0,
        "last_hvac_mode": HVACMode.HEAT,
        "hvac_mode": HVACMode.HEAT,
        "system_mode_received": False,
        "target_temp_received": False,
        "calibration_received": True,
        "last_calibration": 0.0,
        "local_calibration_min": -7.0,
        "local_calibration_max": 7.0,
        "local_calibration_step": 0.5,
        "local_temperature_calibration_entity": "number.trv1_offset",
        "advanced": {
            "calibration_mode": CalibrationMode.DEFAULT,
            "calibration": CalibrationType.LOCAL_BASED,
            "no_off_system_mode": False,
        },
    }
    cfg.update(overrides)
    return Trv.from_legacy_dict("climate.trv1", cfg)


def _make_offset_self(**overrides):
    """Mock BetterThermostat driving one offset-calibrated TRV."""
    return _make_mock_self(
        trv_state=HVACMode.HEAT,
        trv_attrs={"temperature": 20.0},
        real_trvs={"climate.trv1": _offset_trv_config(**overrides)},
    )


async def _run_offset_cycle(
    mock_self, desired_offset, reported_offset, set_offset=None, system_mode=None
):
    """Run one control_trv cycle for the offset-calibrated TRV."""
    if set_offset is None:
        set_offset = AsyncMock(return_value=True)
    with (
        patch(_PATCHES["convert_outbound_states"]) as mock_convert,
        patch(
            _PATCHES["get_current_offset"], new=AsyncMock(return_value=reported_offset)
        ) as mock_get_offset,
        patch(_PATCHES["set_offset"], new=set_offset),
        patch(_PATCHES["set_temperature"], new=AsyncMock()),
        patch(_PATCHES["set_hvac_mode"], new=AsyncMock()),
        patch(_PATCHES["set_valve"], new=AsyncMock()),
        patch(_PATCHES["override_set_hvac_mode"], new=AsyncMock(return_value=False)),
        patch(_PATCHES["override_set_temperature"], new=AsyncMock(return_value=False)),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        mock_convert.return_value = {
            "temperature": 20.0,
            "local_temperature_calibration": desired_offset,
            "local_temperature": 20.0,
            "system_mode": system_mode or HVACMode.HEAT,
        }
        await control_trv(mock_self, "climate.trv1")
    return set_offset, mock_get_offset


def _accepted_write(mock_self):
    """Bookkeeping of an accepted write: the command and the intent.

    The adapter records the value it put on the wire and the delegate
    the value asked for, both before the device has said anything.
    """

    async def _set_offset(_self, entity_id, offset):
        trv = mock_self.real_trvs[entity_id]
        trv.last_calibration = offset
        trv.last_calibration_requested = offset
        return True

    return AsyncMock(side_effect=_set_offset)


class TestOffsetWriteGate:
    """The offset channel re-asserts what the device did not take."""

    @pytest.mark.asyncio
    async def test_unconfirmed_offset_is_written_once_the_report_confirms(self):
        """An unacknowledged write no longer wedges the channel.

        The device reports exactly what it was last told, so nothing is
        in flight; the pending intent must reach it.
        """
        mock_self = _make_offset_self(calibration_received=False, last_calibration=0.0)

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=0.0
        )

        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", -2.0)

    @pytest.mark.asyncio
    async def test_write_arms_the_confirmation_watchdog(self):
        """A write closes the gate and schedules the release that reopens it."""
        mock_self = _make_offset_self(calibration_received=False, last_calibration=0.0)
        tasks = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: (
                (coro.close(), tasks.append(name)) and Mock()
            )
        )

        await _run_offset_cycle(mock_self, desired_offset=-2.0, reported_offset=0.0)

        assert mock_self.real_trvs["climate.trv1"].calibration_received is False
        assert "bt_check_calibration_climate.trv1" in tasks

    @pytest.mark.asyncio
    async def test_silently_dropped_write_is_reasserted_every_released_cycle(self):
        """A device that keeps reporting the old offset is written to again.

        The divergence arm never wedges: each cycle that finds the gate
        open and the report away from the command re-sends the command.
        """
        mock_self = _make_offset_self(calibration_received=True, last_calibration=0.0)
        set_offset = _accepted_write(mock_self)

        for _ in range(3):
            mock_self.real_trvs["climate.trv1"].calibration_received = True
            await _run_offset_cycle(
                mock_self,
                desired_offset=-2.0,
                reported_offset=0.0,
                set_offset=set_offset,
            )
            mock_self.clock.advance(31.0)

        assert set_offset.await_count == 3

    @pytest.mark.asyncio
    async def test_converged_offset_is_not_rewritten(self):
        """Intent, command and report agreeing produces no write."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=-2.0
        )

        set_offset.assert_not_awaited()
        assert mock_self.real_trvs["climate.trv1"].calibration_received is True

    @pytest.mark.asyncio
    async def test_declared_clamp_is_not_rewritten(self):
        """An adapter clamp the device honours is convergence, not a miss.

        The device holds the clamped command it was given, so the intent
        that exceeded its range must not be re-sent on every cycle.
        """
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-3.0,
            last_calibration_requested=-5.0,
        )
        set_offset = AsyncMock(return_value=True)

        for _ in range(5):
            await _run_offset_cycle(
                mock_self,
                desired_offset=-5.0,
                reported_offset=-3.0,
                set_offset=set_offset,
            )
            mock_self.clock.advance(31.0)

        set_offset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_intent_past_a_clamp_is_written_once(self):
        """A changed intent still reaches a device resting at its clamp."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-3.0,
            last_calibration_requested=-5.0,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-6.0, reported_offset=-3.0
        )

        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", -6.0)

    @pytest.mark.asyncio
    async def test_dropped_write_is_reasserted_against_an_unchanged_intent(self):
        """The report, not the intent, decides whether the command arrived."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=0.0
        )

        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", -2.0)

    @pytest.mark.asyncio
    async def test_report_beyond_half_a_step_counts_as_diverged(self):
        """On a fine grid a small deviation is already a lost write."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
            local_calibration_step=0.1,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=-1.7
        )

        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", -2.0)

    @pytest.mark.asyncio
    async def test_report_within_half_a_step_counts_as_confirmed(self):
        """On a coarse grid the same deviation is the device's own snap."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
            local_calibration_step=1.0,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=-2.5
        )

        set_offset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_write_keeps_the_gate_open_and_retries(self):
        """A write the adapter refused arms nothing and is retried."""
        mock_self = _make_offset_self(calibration_received=True, last_calibration=0.0)
        tasks = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: (
                (coro.close(), tasks.append(name)) and Mock()
            )
        )
        set_offset = AsyncMock(return_value=False)

        await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=0.0, set_offset=set_offset
        )

        assert mock_self.real_trvs["climate.trv1"].calibration_received is True
        assert not [name for name in tasks if name.startswith("bt_check_calibration")]

        mock_self.clock.advance(31.0)
        await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=0.0, set_offset=set_offset
        )

        assert set_offset.await_count == 2

    @pytest.mark.asyncio
    async def test_no_calibration_mode_leaves_the_channel_alone(self):
        """NO_CALIBRATION reads no offset, writes none and arms nothing."""
        mock_self = _make_offset_self(
            calibration_received=False,
            last_calibration=0.0,
            advanced={
                "calibration_mode": CalibrationMode.NO_CALIBRATION,
                "calibration": CalibrationType.LOCAL_BASED,
                "no_off_system_mode": False,
            },
        )
        tasks = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: (
                (coro.close(), tasks.append(name)) and Mock()
            )
        )

        set_offset, get_offset = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=0.0
        )

        set_offset.assert_not_awaited()
        get_offset.assert_not_awaited()
        assert not [name for name in tasks if name.startswith("bt_check_calibration")]

    @pytest.mark.asyncio
    async def test_off_mode_leaves_the_channel_alone(self):
        """An OFF TRV keeps its offset even when the report diverged."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self,
            desired_offset=-2.0,
            reported_offset=0.0,
            system_mode=HVACMode.OFF,
        )

        set_offset.assert_not_awaited()
