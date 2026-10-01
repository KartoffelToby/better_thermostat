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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.better_thermostat.adapters import delegate, generic
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.fsm.reachability import ReachabilityState
from custom_components.better_thermostat.core.fsm.window import WindowPhase, WindowState
from custom_components.better_thermostat.core.recorder import FlightRecorder
from custom_components.better_thermostat.core.snapshot import (
    parse_hvac_mode as _parse_mode,
)
from custom_components.better_thermostat.model_fixes import TRVZB, ZWA021
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CONF_HOMEMATICIP,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import (
    HOMEMATICIP_MIN_WRITE_INTERVAL_S,
    MIN_WRITE_INTERVAL_S,
    check_calibration,
    check_target_temperature,
    control_trv,
)
from custom_components.better_thermostat.utils.helpers import resolve_inbound_setpoint
from tests.factories import ThermostatStandIn, make_entity_registry, make_registry_entry

# All delegate / helper functions that control_trv calls.  We patch them at the
# *controlling* module level because that is where they are imported.
_CTRL = "custom_components.better_thermostat.utils.controlling"
_HELPERS = "custom_components.better_thermostat.utils.helpers"
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


@pytest.fixture(autouse=True)
def _no_helper_entity_is_disabled():
    """The entity registry marks none of the TRVs' helper entities disabled.

    The stand-in Home Assistant carries no registry of its own; an empty
    real one answers every helper lookup with "no entry", which the write
    path treats as enabled.
    """
    with patch(f"{_HELPERS}.er.async_get", return_value=make_entity_registry()):
        yield


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

    mock_self = ThermostatStandIn()
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
    mock_self.flight_recorder = FlightRecorder()
    mock_self.control_queue_task = asyncio.Queue(maxsize=1)
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


def _with_valve_channel(trv):
    """Give ``trv`` a writable valve number entity to take valve positions."""
    trv.valve_position_entity = "number.trv1_valve_opening_degree"
    trv.valve_position_writable = True
    trv.adapter = SimpleNamespace(CAPABILITIES=None, set_valve=AsyncMock())
    return trv


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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["set_valve"], autospec=True, return_value=True
            ) as mock_set_valve,
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
            patch(_PATCHES["set_offset"], autospec=True, return_value=True),
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
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
            patch(_PATCHES["set_offset"], autospec=True, return_value=True),
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
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["set_valve"], autospec=True, return_value=True
            ) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
            patch(_PATCHES["set_offset"], autospec=True, return_value=True),
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
    async def test_ignore_trv_states_flag_set_and_reset(self):
        """Test that ignore_trv_states flag is set during processing and reset after."""
        mock_self = _make_mock_self(trv_state=STATE_UNAVAILABLE)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }

            result = await control_trv(mock_self, "climate.trv1")

            assert result is True

    @pytest.mark.asyncio
    async def test_unsupported_heat_mode_writes_only_the_setpoint(self):
        """A device offering no heating mode keeps its mode and gets the setpoint.

        The full outbound conversion runs so the payload really is the one a
        wall thermostat with ``[auto, cool, off]`` produces for a heat demand.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.AUTO,
            trv_attrs={"temperature": 20.0},
            call_for_heat=True,
            bt_target_temp=22.0,
            real_trvs={
                "climate.trv1": _default_trv_config(
                    hvac_modes=[HVACMode.AUTO, HVACMode.COOL, HVACMode.OFF],
                    hvac_mode=HVACMode.AUTO,
                    last_hvac_mode=HVACMode.AUTO,
                )
            },
        )

        with (
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ) as mock_override_mode,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_mode,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await control_trv(mock_self, "climate.trv1")

            mock_override_mode.assert_not_awaited()
            mock_set_mode.assert_not_awaited()
            mock_set_temp.assert_awaited_once_with(mock_self, "climate.trv1", 22.0)

    @pytest.mark.asyncio
    async def test_swapped_device_without_auto_still_gets_heat(self):
        """A swapped valve offering only off/heat is switched to heat.

        The full outbound conversion runs, so this is the payload a device
        sitting in OFF receives on a heat demand.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.OFF,
            trv_attrs={"temperature": 20.0},
            call_for_heat=True,
            bt_target_temp=22.0,
            real_trvs={
                "climate.trv1": _default_trv_config(
                    hvac_modes=[HVACMode.OFF, HVACMode.HEAT],
                    hvac_mode=HVACMode.OFF,
                    last_hvac_mode=HVACMode.OFF,
                    advanced={
                        "calibration_mode": CalibrationMode.NO_CALIBRATION,
                        "calibration": CalibrationType.TARGET_TEMP_BASED,
                        "no_off_system_mode": False,
                        "heat_auto_swapped": True,
                    },
                )
            },
        )

        with (
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_mode,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await control_trv(mock_self, "climate.trv1")

            mock_set_mode.assert_awaited_once_with(
                mock_self, "climate.trv1", HVACMode.HEAT
            )
            mock_set_temp.assert_awaited_once_with(mock_self, "climate.trv1", 22.0)

    @pytest.mark.asyncio
    async def test_set_temperature_quirk_skips_generic_adapter(self):
        """A model quirk that handles the write suppresses the adapter call."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=True
            ) as mock_override,
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ) as mock_override,
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ) as mock_override,
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
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
                "climate.trv1": _with_valve_channel(
                    _default_trv_config(
                        advanced={
                            "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                            "calibration": CalibrationType.DIRECT_VALVE_BASED,
                            "no_off_system_mode": False,
                        }
                    )
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_valve"], autospec=True, return_value=True
            ) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ) as mock_override,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ) as mock_override,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True),
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
    async def test_a_refused_mode_is_not_taken_as_the_device_mode(self):
        """A refused mode is retried, and the device keeps the mode it reports.

        The mode last commanded is what the end-of-cycle refresh caches and
        what the inbound handler compares a report against. A refused
        command recorded there would read the device's next plain report as
        a press back to its old mode and undo the user's choice.
        """
        trv = _default_trv_config(
            last_hvac_mode=HVACMode.OFF, system_mode_received=True
        )
        mock_self = _make_mock_self(
            trv_state=HVACMode.OFF,
            trv_attrs={"temperature": 20.0},
            real_trvs={"climate.trv1": trv},
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_hvac_mode"], autospec=True, return_value=False
            ) as mock_set_hvac,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")
            await control_trv(mock_self, "climate.trv1")

        assert [c.args[2] for c in mock_set_hvac.await_args_list] == [
            HVACMode.HEAT,
            HVACMode.HEAT,
        ]
        assert trv.last_hvac_mode == HVACMode.OFF
        assert trv.system_mode_received is True
        assert not any(
            call.kwargs.get("name") == "bt_check_system_mode_climate.trv1"
            for call in mock_self.task_manager.create_task.call_args_list
        )

    @pytest.mark.asyncio
    async def test_a_refused_mode_on_a_trv_reading_unknown_keeps_the_old_command(self):
        """A driven Spirit reads ``unknown``; that is no mode it holds.

        The device is operated while its entity reads ``unknown``, so the
        cycle writes its mode. A refusal there leaves the mode last commanded
        where it was rather than recording ``unknown`` as one.
        """
        trv = _default_trv_config(
            model="Spirit",
            model_quirks=ZWA021,
            last_hvac_mode=HVACMode.HEAT,
            system_mode_received=True,
            advanced={
                "calibration_mode": CalibrationMode.NO_CALIBRATION,
                "calibration": CalibrationType.DIRECT_VALVE_BASED,
                "no_off_system_mode": False,
            },
        )
        mock_self = _make_mock_self(
            trv_state=STATE_UNKNOWN,
            trv_attrs={"temperature": 21.0, "hvac_modes": ["heat", "off"]},
            real_trvs={"climate.trv1": trv},
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["set_hvac_mode"], autospec=True, return_value=False
            ) as mock_set_hvac,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "system_mode": HVACMode.HEAT,
                "temperature": 21.0,
            }

            await control_trv(mock_self, "climate.trv1")

        mock_set_hvac.assert_awaited_once()
        assert trv.last_hvac_mode == HVACMode.HEAT

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
                "climate.trv1": _with_valve_channel(
                    _default_trv_config(
                        system_mode_received=True,
                        advanced={
                            "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                            "calibration": CalibrationType.DIRECT_VALVE_BASED,
                            "no_off_system_mode": False,
                        },
                    )
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
                _PATCHES["set_valve"], autospec=True, side_effect=drop_trv_offline
            ) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ) as mock_override_hvac,
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True),
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }

            await control_trv(mock_self, "climate.trv1")

            # Lock should have been acquired
            lock_acquire_mock.assert_awaited()

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
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ) as mock_override,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True),
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
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
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
            mock_set_hvac.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_offered_in_the_device_spelling_switches_the_device_off(self):
        """A list naming its modes ``HVACMode.OFF`` still offers OFF.

        The cached list holds the device's own spelling, so the min_temp
        substitution must not fire for a device that does offer OFF.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            call_for_heat=False,
            real_trvs={
                "climate.trv1": _default_trv_config(
                    hvac_modes=["HVACMode.OFF", "HVACMode.HEAT"]
                )
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            await control_trv(mock_self, "climate.trv1")

            mock_set_hvac.assert_awaited_once()
            assert mock_set_hvac.await_args[0][2] == HVACMode.OFF
            for call in mock_set_temp.call_args_list:
                assert call[0][2] != 5.0

    @pytest.mark.asyncio
    async def test_no_off_in_the_device_spelling_still_sends_min_temp(self):
        """A device genuinely without OFF keeps taking the min_temp path."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            call_for_heat=False,
            real_trvs={
                "climate.trv1": _default_trv_config(hvac_modes=["HVACMode.HEAT"])
            },
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            await control_trv(mock_self, "climate.trv1")

            mock_set_temp.assert_called_once()
            assert mock_set_temp.call_args[0][2] == 5.0
            mock_set_hvac.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_hvac_modes_does_not_crash(self):
        """A TRV reporting no hvac_modes (None) must not crash control_trv.

        ``Trv.hvac_modes`` defaults to None and stays None when a TRV exposes
        no ``hvac_modes`` attribute. The membership check for HVACMode.OFF must
        tolerate that instead of raising TypeError; a None list is treated as
        "no OFF mode available", so an OFF request falls back to min_temp.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            call_for_heat=False,  # No heat needed -> OFF
            real_trvs={"climate.trv1": _default_trv_config(hvac_modes=None)},
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            mock_set_temp.return_value = None

            await control_trv(mock_self, "climate.trv1")

            # None hvac_modes -> treated as no OFF mode -> min_temp (5.0) sent.
            mock_set_temp.assert_called_once()
            assert mock_set_temp.call_args[0][2] == 5.0
            mock_set_hvac.assert_not_awaited()


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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(
                _PATCHES["set_temperature"],
                autospec=True,
                side_effect=RuntimeError("adapter failure"),
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(
                _PATCHES["set_temperature"],
                autospec=True,
                side_effect=_blocking_set_temperature,
            ),
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
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(
                _PATCHES["set_temperature"],
                autospec=True,
                side_effect=_blocking_set_temperature,
            ),
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

        mock_self = ThermostatStandIn()
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
        mock_self.flight_recorder = FlightRecorder()
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
            patch(_PATCHES["set_valve"], autospec=True, side_effect=track_set_valve),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
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
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["set_valve"], autospec=True, return_value=True
            ) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
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
            patch(_PATCHES["set_valve"], autospec=True, side_effect=track_set_valve),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
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
                "climate.trv1": _with_valve_channel(
                    _default_trv_config(
                        advanced={
                            "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                            "calibration": CalibrationType.DIRECT_VALVE_BASED,
                            "no_off_system_mode": False,
                        }
                    )
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
            patch(_PATCHES["set_valve"], autospec=True, side_effect=failing_set_valve),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
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
        assert mock_self.control_queue_task.get_nowait() is mock_self
        assert mock_self.real_trvs["climate.trv1"].budget_retry_pending is False

        # Close any other captured coroutines to avoid RuntimeWarning.
        for coro, name in captured:
            if "budget_retry" not in (name or ""):
                coro.close()

    @pytest.mark.asyncio
    async def test_failed_safety_reset_retries_at_the_normal_pace_on_homematicip(self):
        """A failed 0% safety reset is retried after the normal spacing.

        The reset bypasses the budget, so its retry must not wait out the
        HomematicIP head's longer interval with the valve still open.
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
                        CONF_HOMEMATICIP: True,
                    }
                )
            },
        )
        mock_self.last_user_change_monotonic = None

        captured = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: captured.append((coro, name)) or Mock()
        )

        async def failing_set_valve(*args, **kwargs):
            # The boost 100% write succeeds; the 0% safety reset fails.
            return args[2] != 0

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_valve"], autospec=True, side_effect=failing_set_valve),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

        delays = []

        async def _sleep(seconds):
            delays.append(seconds)

        for coro, name in captured:
            if "budget_retry" in (name or ""):
                with patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep)):
                    await coro
            else:
                coro.close()
        assert delays == [pytest.approx(MIN_WRITE_INTERVAL_S)]

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

        mock_self = ThermostatStandIn()
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
        mock_self.flight_recorder = FlightRecorder()
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
            patch(_PATCHES["set_valve"], autospec=True, side_effect=track_set_valve),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
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


class TestValveWriteResult:
    """The valve channel acts on what the delegate answers.

    A write the delegate refused still stamped the budget slot, so the
    next cycle has to be requested explicitly. Without it the valve keeps
    the position the device never took until an unrelated event triggers
    control.
    """

    @staticmethod
    def _boost_valve_self():
        """Mock BetterThermostat whose boost drives a 100 % valve write."""
        return _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            preset_mode=PRESET_BOOST,
            cur_temp=18.0,
            bt_target_temp=22.0,
            real_trvs={
                "climate.trv1": _with_valve_channel(
                    _default_trv_config(
                        advanced={
                            "calibration_mode": CalibrationMode.MPC_CALIBRATION,
                            "calibration": CalibrationType.DIRECT_VALVE_BASED,
                            "no_off_system_mode": False,
                        }
                    )
                )
            },
        )

    @staticmethod
    async def _run_cycle(mock_self, valve_result, write=None):
        """Run one control cycle with the delegate answering ``valve_result``.

        ``write``, when given, stands in for the delegate's answer and is
        what the valve write runs through.

        Returns the valve mock and the names of the tasks the cycle
        created.
        """
        captured = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: captured.append((coro, name)) or Mock()
        )

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_valve"],
                autospec=True,
                return_value=valve_result,
                side_effect=write,
            ) as mock_set_valve,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

        for coro, _name in captured:
            coro.close()
        return mock_set_valve, [name for _coro, name in captured]

    @pytest.mark.asyncio
    async def test_a_refused_valve_write_schedules_a_retry_cycle(self):
        """A delegate answering False leaves a follow-up cycle queued."""
        mock_self = self._boost_valve_self()

        mock_set_valve, task_names = await self._run_cycle(mock_self, False)

        assert mock_set_valve.call_args[0][2] == 100
        assert "bt_budget_retry_climate.trv1" in task_names
        assert mock_self.real_trvs["climate.trv1"].budget_retry_pending is True

    @pytest.mark.asyncio
    async def test_an_accepted_valve_write_schedules_nothing(self):
        """A delegate answering True needs no catch-up cycle."""
        mock_self = self._boost_valve_self()

        mock_set_valve, task_names = await self._run_cycle(mock_self, True)

        assert mock_set_valve.call_args[0][2] == 100
        assert "bt_budget_retry_climate.trv1" not in task_names
        assert mock_self.real_trvs["climate.trv1"].budget_retry_pending is False

    @pytest.mark.asyncio
    async def test_a_disabled_valve_entity_is_not_pursued_until_enabled(self):
        """A valve entity disabled in Home Assistant is no valve channel.

        Disabling lasts until the user acts, so it is not a failed write:
        the cycle neither writes the position nor queues a catch-up cycle
        for it, however many cycles run. Once the entity is enabled again,
        the next cycle writes the position.
        """
        mock_self = self._boost_valve_self()
        valve = "number.trv1_valve_opening_degree"
        mock_self.real_trvs["climate.trv1"].valve_position_entity = valve
        mock_self.real_trvs["climate.trv1"].valve_position_writable = True
        disabled = make_entity_registry(
            make_registry_entry(valve, disabled_by=er.RegistryEntryDisabler.USER)
        )
        enabled = make_entity_registry(make_registry_entry(valve))

        with patch(f"{_HELPERS}.er.async_get", return_value=disabled):
            for _ in range(3):
                # Each cycle finds every write slot open, so a catch-up
                # cycle could only be asked for on the valve's behalf.
                mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
                mock_set_valve, task_names = await self._run_cycle(mock_self, False)
                mock_set_valve.assert_not_called()
                assert "bt_budget_retry_climate.trv1" not in task_names
        assert mock_self.real_trvs["climate.trv1"].budget_retry_pending is False

        with patch(f"{_HELPERS}.er.async_get", return_value=enabled):
            mock_set_valve, _ = await self._run_cycle(mock_self, True)
        assert mock_set_valve.call_args[0][2] == 100

    @pytest.mark.asyncio
    async def test_a_quirk_still_writes_the_valve_past_a_disabled_entity(self):
        """A model quirk with its own valve write keeps the valve in reach.

        Such a quirk writes through its own channel, not the adopted valve
        entity, so disabling that entity leaves the valve position pursued
        and the quirk takes it.
        """
        mock_self = self._boost_valve_self()
        trv = mock_self.real_trvs["climate.trv1"]
        valve = "number.trv1_valve_opening_degree"
        trv.valve_position_entity = valve
        trv.valve_position_writable = True
        quirk_write = AsyncMock(return_value=True)
        trv.model_quirks = SimpleNamespace(override_set_valve=quirk_write)
        disabled = make_entity_registry(
            make_registry_entry(valve, disabled_by=er.RegistryEntryDisabler.USER)
        )

        with patch(f"{_HELPERS}.er.async_get", return_value=disabled):
            mock_set_valve, task_names = await self._run_cycle(mock_self, True)
            assert mock_set_valve.call_args[0][2] == 100
            assert "bt_budget_retry_climate.trv1" not in task_names

            assert await delegate.set_valve(mock_self, "climate.trv1", 100) is True
        quirk_write.assert_awaited_once_with(mock_self, "climate.trv1", 100)
        assert trv.last_valve_method == "override"

    OPENING = "number.trv1_valve_opening_degree"

    def _trvzb_self(self, opening=None):
        """A boosting TRVZB whose device carries the ``opening`` number entry.

        Returns the thermostat and the registry holding the TRV and, when
        given, the opening number on the TRV's device.
        """
        mock_self = self._boost_valve_self()
        mock_self.hass.services.async_call = AsyncMock()
        trv = mock_self.real_trvs["climate.trv1"]
        trv.model = "TRVZB"
        trv.model_quirks = TRVZB
        trv.adapter = SimpleNamespace(CAPABILITIES=None, set_valve=AsyncMock())
        trv.valve_position_entity = None
        trv.valve_position_writable = None
        entries = [make_registry_entry("climate.trv1", device_id="trvzb")]
        if opening is not None:
            trv.valve_position_entity = opening.entity_id
            trv.valve_position_writable = True
            entries.append(opening)
        return mock_self, make_entity_registry(*entries)

    def _opening(self, **fields):
        return make_registry_entry(
            self.OPENING,
            device_id="trvzb",
            translation_key="valve_opening_degree",
            **fields,
        )

    async def _cycles(self, mock_self, registry, count):
        """Run ``count`` cycles through the real valve write; collect their tasks."""
        calls, task_names = 0, []
        with patch(f"{_HELPERS}.er.async_get", return_value=registry):
            for _ in range(count):
                mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
                mock_set_valve, names = await self._run_cycle(
                    mock_self, None, write=delegate.set_valve
                )
                calls += mock_set_valve.await_count
                task_names += names
        return calls, task_names

    @pytest.mark.asyncio
    async def test_a_trvzb_with_its_opening_number_disabled_is_left_alone(self, caplog):
        """A TRVZB whose valve opening number is disabled has no valve channel.

        Its quirk writes only to that number, so the valve position is not
        pursued and no catch-up cycle is queued, and the disabled entity is
        named once.
        """
        mock_self, registry = self._trvzb_self(
            self._opening(disabled_by=er.RegistryEntryDisabler.USER)
        )

        calls, task_names = await self._cycles(mock_self, registry, 3)

        assert calls == 0
        assert "bt_budget_retry_climate.trv1" not in task_names
        warnings = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "disabled" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert self.OPENING in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_trvzb_without_an_opening_number_is_left_alone(self):
        """A TRVZB whose device offers no valve number has no valve channel.

        Nothing but the user adding such an entity changes that, so no
        catch-up cycle is queued for the valve.
        """
        mock_self, registry = self._trvzb_self()

        calls, task_names = await self._cycles(mock_self, registry, 3)

        assert calls == 0
        assert "bt_budget_retry_climate.trv1" not in task_names

    @pytest.mark.asyncio
    async def test_a_trvzb_with_an_enabled_opening_number_is_written(self):
        """A TRVZB's quirk writes the boost position to its opening number."""
        mock_self, registry = self._trvzb_self(self._opening())

        calls, task_names = await self._cycles(mock_self, registry, 1)

        assert calls == 1
        assert "bt_budget_retry_climate.trv1" not in task_names
        mock_self.hass.services.async_call.assert_any_await(
            "number",
            "set_value",
            {"entity_id": self.OPENING, "value": 100},
            blocking=True,
            context=mock_self.context,
        )
        assert mock_self.real_trvs["climate.trv1"].last_valve_method == "override"


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

        mock_self = ThermostatStandIn()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_grouped_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
        mock_self.clock = FakeClock()
        mock_self.flight_recorder = FlightRecorder()
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
            patch(
                _PATCHES["set_valve"], autospec=True, return_value=True
            ) as mock_set_valve,
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac_mode,
            patch(
                _PATCHES["set_offset"], autospec=True, return_value=True
            ) as mock_set_offset,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
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
                return True

            async def delayed_set_hvac_mode(*args, **kwargs):
                execution_log.append(f"set_hvac_mode_start_{args[1]}")
                await asyncio.sleep(0.01)
                execution_log.append(f"set_hvac_mode_end_{args[1]}")

            async def delayed_set_offset(*args, **kwargs):
                execution_log.append(f"set_offset_start_{args[1]}")
                await asyncio.sleep(0.01)
                execution_log.append(f"set_offset_end_{args[1]}")
                return True

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

        mock_self = ThermostatStandIn()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
        mock_self.clock = FakeClock()
        mock_self.flight_recorder = FlightRecorder()
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
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
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

        mock_self = ThermostatStandIn()
        mock_self.hass = mock_hass
        mock_self.device_name = "test_thermostat"
        mock_self._temp_lock = asyncio.Lock()
        mock_self.calculate_heating_power = AsyncMock()
        mock_self.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
        mock_self.clock = FakeClock()
        mock_self.flight_recorder = FlightRecorder()
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
            patch(
                _PATCHES["set_valve"], autospec=True, return_value=True
            ) as mock_set_valve,
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac_mode,
            patch(
                _PATCHES["set_offset"], autospec=True, return_value=True
            ) as mock_set_offset,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["get_current_offset"], autospec=True, return_value=0.0),
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
                return True

            async def check_lock_on_set_hvac_mode(*args, **kwargs):
                lock_state_during_operations.append(
                    ("set_hvac_mode", mock_self._temp_lock.locked())
                )

            async def check_lock_on_set_offset(*args, **kwargs):
                lock_state_during_operations.append(
                    ("set_offset", mock_self._temp_lock.locked())
                )
                return True

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
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["set_temperature"],
                autospec=True,
                side_effect=lambda *a, **k: set_temperature_calls.append(a),
            ),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
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
    bt = ThermostatStandIn()
    bt.hass = MagicMock()
    bt.clock = FakeClock()
    bt.flight_recorder = FlightRecorder()
    bt.preset_mode = None
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
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_offset"], autospec=True, return_value=True
            ) as mock_set_offset,
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
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
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_offset"], autospec=True, return_value=True
            ) as mock_set_offset,
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
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
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["set_offset"], autospec=True, return_value=True
            ) as mock_set_offset,
            patch(_PATCHES["set_temperature"], autospec=True) as mock_set_temp,
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
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
            mock_set_temp.assert_awaited_once_with(mock_bt_grouped, entity_id, 21.0)

    @pytest.mark.parametrize(
        ("step", "reported", "released"),
        [(0.5, 2.5, True), (0.5, 2.6, False), (1.0, 3.0, True), (1.0, 3.1, False)],
    )
    async def test_confirmation_window_is_one_device_step(
        self, mock_bt_grouped, step, reported, released
    ):
        """A report within one of the device's own offset steps confirms.

        The window is symmetric because the gate cannot tell a truncated
        count from a device holding its previous value one step away,
        and one step is the smallest correction the channel issues; a
        report beyond one step is a lost write.
        """
        entity_id = "climate.trv_3"
        mock_bt_grouped.real_trvs[entity_id].local_calibration_step = step

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], autospec=True, return_value=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
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

    async def test_calibration_tolerance_outside_half_degree(self, mock_bt_grouped):
        """Test that calibration outside 0.5 degree tolerance is not matching."""
        entity_id = "climate.trv_3"

        mock_trv_state = MagicMock()
        mock_trv_state.state = "heat"
        mock_trv_state.attributes = {"temperature": 21.0}
        mock_bt_grouped.hass.states.get.return_value = mock_trv_state

        with (
            patch(
                _PATCHES["get_current_offset"], autospec=True, return_value=0.0
            ) as mock_get_offset,
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(_PATCHES["set_offset"], autospec=True, return_value=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
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


class TestControlTrvOnADualRoleEntity:
    """The outbound mode a device that carries both roles receives.

    HEAT_COOL is the mode a room with a cooler runs in, and it names a pair of
    targets Better Thermostat holds. Handing that mode to a device that can act
    on both sides of it hands the room to that device's own thermostat.
    """

    SHARED_ID = "climate.reversible_ac"

    @classmethod
    def _make_shared_self(cls, device_modes, *, heat_auto_swapped=False):
        """Build a mock whose cooler is also the controlled thermostat."""
        trv = Trv.from_legacy_dict(
            cls.SHARED_ID,
            {
                "ignore_trv_states": False,
                "hvac_modes": device_modes,
                "min_temp": 16.0,
                "max_temp": 30.0,
                "temperature": 21.0,
                "last_temperature": 21.0,
                "last_hvac_mode": HVACMode.HEAT,
                "current_temperature": 19.0,
                "hvac_mode": HVACMode.HEAT,
                "advanced": {
                    "calibration_mode": CalibrationMode.NO_CALIBRATION,
                    "calibration": CalibrationType.TARGET_TEMP_BASED,
                    "no_off_system_mode": False,
                    "heat_auto_swapped": heat_auto_swapped,
                },
            },
        )
        # The device reports OFF, so every candidate outbound mode differs
        # from it and reaches set_hvac_mode where the test can read it.
        return _make_mock_self(
            trv_state=HVACMode.OFF,
            trv_attrs={"temperature": 21.0},
            real_trvs={cls.SHARED_ID: trv},
            bt_hvac_mode=HVACMode.HEAT,
            cooler_entity_id=cls.SHARED_ID,
            bt_target_temp=21.0,
        )

    @staticmethod
    async def _outbound_system_mode(mock_self, entity_id):
        """Run one control_trv cycle and return the mode it wrote out."""
        with (
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True) as mock_set_hvac,
            patch(_PATCHES["set_temperature"], autospec=True),
            patch(_PATCHES["set_valve"], autospec=True, return_value=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await control_trv(mock_self, entity_id)
            if not mock_set_hvac.await_args_list:
                return None
            return mock_set_hvac.await_args_list[-1].args[2]

    @pytest.mark.asyncio
    async def test_dual_role_entity_is_sent_heat_not_heat_cool(self):
        """An air conditioner that advertises heat_cool receives heat in a heating cycle.

        The room is on, which a room with a cooler publishes as heat_cool. The
        device's own thermostat would otherwise run the room against its own
        pair of setpoints for the whole cycle the heating channel owns it.
        """
        mock_self = self._make_shared_self(
            [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
        )

        assert await self._outbound_system_mode(mock_self, self.SHARED_ID) == (
            HVACMode.HEAT
        )

    @pytest.mark.asyncio
    async def test_a_heat_cool_only_device_is_still_sent_heat_cool(self):
        """A device without a heat mode keeps the translation it depends on."""
        mock_self = self._make_shared_self([HVACMode.OFF, HVACMode.HEAT_COOL])

        assert await self._outbound_system_mode(mock_self, self.SHARED_ID) == (
            HVACMode.HEAT_COOL
        )

    @pytest.mark.asyncio
    async def test_heat_auto_swapped_is_still_honoured_for_a_dual_role_entity(self):
        """The quirk that maps heat onto auto still reaches the device."""
        mock_self = self._make_shared_self(
            [HVACMode.OFF, HVACMode.AUTO], heat_auto_swapped=True
        )

        assert await self._outbound_system_mode(mock_self, self.SHARED_ID) == (
            HVACMode.AUTO
        )

    @pytest.mark.asyncio
    async def test_a_distinct_trv_offering_heat_cool_receives_heat(self):
        """A radiator that is not the cooler is sent heat in a heat_cool room.

        A radiator that offers heat_cool as well would run its own thermostat
        in it, so it is driven in the mode it heats in.
        """
        mock_self = self._make_shared_self(
            [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL]
        )
        mock_self.cooler_entity_id = "climate.split_unit"

        assert await self._outbound_system_mode(mock_self, self.SHARED_ID) == (
            HVACMode.HEAT
        )


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
            _PATCHES["get_current_offset"], autospec=True, return_value=reported_offset
        ) as mock_get_offset,
        patch(_PATCHES["set_offset"], autospec=True, side_effect=set_offset),
        patch(_PATCHES["set_temperature"], autospec=True),
        patch(_PATCHES["set_hvac_mode"], autospec=True),
        patch(_PATCHES["set_valve"], autospec=True, return_value=True),
        patch(_PATCHES["override_set_hvac_mode"], autospec=True, return_value=False),
        patch(_PATCHES["override_set_temperature"], autospec=True, return_value=False),
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
    async def test_a_disabled_calibration_entity_is_not_pursued_until_enabled(self):
        """A calibration entity disabled in Home Assistant is no offset channel.

        Disabling lasts until the user acts, so the cycle neither reads nor
        writes the offset and takes no write slot for it. Once the entity
        is enabled again, the next cycle writes the offset.
        """
        mock_self = _make_offset_self()
        captured = []
        mock_self.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: (
                (coro.close(), captured.append(name)) and Mock()
            )
        )
        offset_entity = "number.trv1_offset"
        disabled = make_entity_registry(
            make_registry_entry(
                offset_entity, disabled_by=er.RegistryEntryDisabler.USER
            )
        )
        enabled = make_entity_registry(make_registry_entry(offset_entity))

        with patch(f"{_HELPERS}.er.async_get", return_value=disabled):
            for _ in range(3):
                mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
                set_offset, get_offset = await _run_offset_cycle(
                    mock_self, desired_offset=-2.0, reported_offset=0.0
                )
                set_offset.assert_not_awaited()
                get_offset.assert_not_called()
        assert mock_self.real_trvs["climate.trv1"].last_offset_write_monotonic is None
        assert "bt_budget_retry_climate.trv1" not in captured

        with patch(f"{_HELPERS}.er.async_get", return_value=enabled):
            set_offset, _ = await _run_offset_cycle(
                mock_self, desired_offset=-2.0, reported_offset=0.0
            )
        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", -2.0)

    @pytest.mark.asyncio
    async def test_unconfirmed_offset_is_written_once_the_report_confirms(self):
        """An unacknowledged write leaves the channel open.

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
    async def test_report_beyond_a_step_counts_as_diverged(self):
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
    async def test_report_within_a_step_counts_as_confirmed(self):
        """On a coarse grid a whole step of deviation is still the command."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
            local_calibration_step=1.0,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-2.0, reported_offset=-3.0
        )

        set_offset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_report_one_step_below_the_command_is_not_rewritten(self):
        """A truncated offset count is the command, not a dropped write.

        A value written as 6.3 arrives as ``int(6.3 / 0.1) == 62`` counts
        on a 0.1 K device, so it holds and reports 6.2 for as long as the
        intent stands. Rewriting it would send the same command every
        cycle and wait out the confirmation window each time.
        """
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=6.3,
            last_calibration_requested=6.3,
            local_calibration_step=0.1,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=6.3, reported_offset=6.2
        )

        set_offset.assert_not_awaited()
        assert mock_self.real_trvs["climate.trv1"].calibration_received is True

    @pytest.mark.asyncio
    async def test_report_two_steps_below_the_command_is_rewritten(self):
        """Two steps of distance is a dropped write on a 0.1 K grid."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=6.3,
            last_calibration_requested=6.3,
            local_calibration_step=0.1,
        )

        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=6.3, reported_offset=6.1
        )

        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", 6.3)

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

    @pytest.mark.asyncio
    async def test_declared_step_finer_than_the_reported_grid_converges(self):
        """A device rounding coarser than it declares is written once.

        The device declares a 0.01 K offset step but publishes its offset
        on a 0.05 K grid. Half the declared step is narrower than that
        grid, so without the floor every cycle would read the rounding as
        a divergence and re-assert the same command forever.
        """
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=0.0,
            last_calibration_requested=0.0,
            local_calibration_step=0.01,
        )
        reported = {"value": 0.0}

        async def _write_and_round(_self, entity_id, offset):
            """Take the command and publish it on the device's own grid."""
            trv = mock_self.real_trvs[entity_id]
            trv.last_calibration = offset
            trv.last_calibration_requested = offset
            reported["value"] = round(round(offset / 0.05) * 0.05, 6)
            return True

        set_offset = AsyncMock(side_effect=_write_and_round)
        for _ in range(20):
            mock_self.real_trvs["climate.trv1"].calibration_received = True
            await _run_offset_cycle(
                mock_self,
                desired_offset=-2.32,
                reported_offset=reported["value"],
                set_offset=set_offset,
            )
            mock_self.clock.advance(31.0)

        assert set_offset.await_count == 1

    @pytest.mark.asyncio
    async def test_each_write_arms_a_watchdog_for_its_own_command(self):
        """Every accepted write takes the next generation."""
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=0.0,
            last_calibration_requested=0.0,
        )
        watchdog = Mock(return_value=None)

        with patch(f"{_CTRL}.check_calibration", new=watchdog):
            for _ in range(3):
                mock_self.real_trvs["climate.trv1"].calibration_received = True
                await _run_offset_cycle(
                    mock_self, desired_offset=-2.0, reported_offset=0.0
                )
                mock_self.clock.advance(31.0)

        assert [call.args[2] for call in watchdog.call_args_list] == [1, 2, 3]
        assert mock_self.real_trvs["climate.trv1"].calibration_write_generation == 3

    @pytest.mark.asyncio
    async def test_a_superseded_watchdog_does_not_reopen_the_gate(self):
        """The gate stays closed while the newest command is unconfirmed.

        A control cycle can confirm a command in-cycle and immediately
        write a newer one, leaving the earlier watchdog winding down. Its
        release must not open the channel for the write still in flight.
        """
        mock_self = _make_offset_self(
            calibration_received=False,
            last_calibration=-2.0,
            last_calibration_requested=-2.0,
        )
        trv = mock_self.real_trvs["climate.trv1"]
        trv.calibration_write_generation = 1
        earlier_watchdog = check_calibration(mock_self, "climate.trv1", 1)

        # The cycle confirms -2.0 and writes -3.0, arming the newer watchdog.
        set_offset, _ = await _run_offset_cycle(
            mock_self, desired_offset=-3.0, reported_offset=-2.0
        )

        set_offset.assert_awaited_once_with(mock_self, "climate.trv1", -3.0)
        assert trv.calibration_received is False
        assert trv.calibration_write_generation == 2

        with patch(f"{_CTRL}.get_current_offset", autospec=True, return_value=-2.0):
            assert await earlier_watchdog is True

        assert trv.calibration_received is False


SELECT_CALIBRATION_ENTITY = "select.trv1_calibration"
# A calibration select on a 3 K grid: an offset between two options reaches
# the device as the nearer one.
SELECT_OPTIONS = ["-6.0k", "-3.0k", "0.0k", "3.0k", "6.0k"]


class _SnappingSelect:
    """A calibration select that holds exactly the option it was handed."""

    def __init__(self, selected="0.0k"):
        """Start the entity on the option it currently holds.

        Parameters
        ----------
        selected : str
            Option the entity holds before the first write.
        """
        self.selected = selected
        self.written = []

    @property
    def reported(self):
        """Return the offset in Kelvin the entity publishes."""
        return float(self.selected.replace("k", ""))

    def state(self):
        """Return the entity state the adapter reads its options off."""
        return State(
            SELECT_CALIBRATION_ENTITY, self.selected, {"options": list(SELECT_OPTIONS)}
        )

    async def async_call(self, domain, service, data, **kwargs):
        """Apply a select_option call and ignore every other service.

        Parameters
        ----------
        domain : str
            Service domain of the call.
        service : str
            Service name within that domain.
        data : dict
            Service data; carries the option for a select_option call.
        **kwargs : dict
            Blocking and context arguments the caller passes on.
        """
        if domain == "select" and service == "select_option":
            self.written.append(data["option"])
            self.selected = data["option"]


class TestSnappingSelectOffsetConverges:
    """A device that snaps the offset onto its own option list settles.

    The adapter, not the device, decides which option carries the offset,
    so the command the gate confirms against is the option's value. The
    intent stays what the cycle asked for, so an unchanged intent does not
    re-arm the write once the device holds the snapped option.
    """

    def _wire(self, device):
        """Return a thermostat whose calibration entity is ``device``.

        Parameters
        ----------
        device : _SnappingSelect
            The calibration select standing in for the TRV's offset channel.

        Returns
        -------
        Mock
            A stand-in for the Better Thermostat climate entity instance.
        """
        mock_self = _make_offset_self(
            calibration_received=True,
            last_calibration=0.0,
            last_calibration_requested=0.0,
            local_temperature_calibration_entity=SELECT_CALIBRATION_ENTITY,
            # A select publishes no step, so discovery falls back to 1.0.
            local_calibration_step=1.0,
        )
        mock_self.real_trvs["climate.trv1"].adapter = generic
        trv_state = mock_self.hass.states.get.return_value
        mock_self.hass.states.get.side_effect = lambda entity_id: (
            device.state() if entity_id == SELECT_CALIBRATION_ENTITY else trv_state
        )
        mock_self.hass.services.async_call = AsyncMock(side_effect=device.async_call)
        return mock_self

    @pytest.mark.asyncio
    async def test_the_snapped_option_is_written_once(self):
        """Five cycles against a device holding the snapped option write once."""
        device = _SnappingSelect()
        mock_self = self._wire(device)
        trv = mock_self.real_trvs["climate.trv1"]
        # The real write path: the delegate records the intent and the
        # adapter the command.
        set_offset = AsyncMock(side_effect=delegate.set_offset)

        for _ in range(5):
            # The watchdog releases the gate at the end of its window.
            trv.calibration_received = True
            await _run_offset_cycle(
                mock_self,
                desired_offset=-2.0,
                reported_offset=device.reported,
                set_offset=set_offset,
            )
            mock_self.clock.advance(31.0)

        assert device.written == ["-3.0k"]
        assert trv.last_calibration == -3.0
        assert trv.last_calibration_requested == -2.0

    @pytest.mark.asyncio
    async def test_a_new_intent_still_reaches_the_device(self):
        """A cycle asking for another option writes it."""
        device = _SnappingSelect()
        mock_self = self._wire(device)
        trv = mock_self.real_trvs["climate.trv1"]
        set_offset = AsyncMock(side_effect=delegate.set_offset)

        for desired in (-2.0, 2.0):
            trv.calibration_received = True
            await _run_offset_cycle(
                mock_self,
                desired_offset=desired,
                reported_offset=device.reported,
                set_offset=set_offset,
            )
            mock_self.clock.advance(31.0)

        assert device.written == ["-3.0k", "3.0k"]


# ---------------------------------------------------------------------------
# Echo bookkeeping across control cycles
# ---------------------------------------------------------------------------


class TestEchoSetpointBookkeeping:
    """The writes a device may still echo follow the control cycle."""

    @pytest.mark.asyncio
    async def test_the_writes_since_the_confirmed_one_are_remembered(self):
        """A confirmed write and the one after it are both remembered.

        The cycle writes 26.0 and the device confirms it, so 26.0 becomes the
        confirmed setpoint and the write list empties. The next cycle writes
        25.0; a device that holds on to 26.0 then reports a value BT wrote,
        so 26.0 stays known next to the new command.
        """
        trv_attrs = {"temperature": 20.0}
        mock_self = _make_mock_self(trv_state=HVACMode.HEAT, trv_attrs=trv_attrs)
        trv = mock_self.real_trvs["climate.trv1"]

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 26.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")
            assert trv.echo_setpoint_values() == [26.0]

            trv_attrs["temperature"] = 26.0
            await check_target_temperature(
                mock_self, "climate.trv1", trv.last_setpoint_write_id, 26.0
            )
            assert trv.confirmed_setpoint == 26.0
            assert trv.echo_setpoint_values() == []

            mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
            mock_convert.return_value = {
                "temperature": 25.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

        assert trv.last_temperature == 25.0
        assert trv.confirmed_setpoint == 26.0
        assert trv.echo_setpoint_values() == [25.0]

    @pytest.mark.asyncio
    async def test_each_write_in_a_cycle_takes_a_rising_id(self):
        """The id the watchdog captures is the one of the write it follows."""
        trv_attrs = {"temperature": 20.0}
        mock_self = _make_mock_self(trv_state=HVACMode.HEAT, trv_attrs=trv_attrs)
        trv = mock_self.real_trvs["climate.trv1"]

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 23.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")
            # The watchdog task is created inside that cycle, so this is the
            # id it reads at entry.
            awaited_write_id = trv.last_setpoint_write_id

            for value in (24.0, 25.0):
                mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
                mock_convert.return_value = {
                    "temperature": value,
                    "system_mode": HVACMode.HEAT,
                }
                await control_trv(mock_self, "climate.trv1")

        assert trv.echo_setpoint_values() == [23.0, 24.0, 25.0]
        assert trv.last_setpoint_write_id > awaited_write_id

    @pytest.mark.asyncio
    async def test_the_value_the_delegate_sent_is_remembered_next_to_the_intent(self):
        """Both the intent and the value the delegate sent may echo.

        The cycle asks for 20.7 on a device with a 0.5 step; the delegate
        rounds and sends 20.5, which is what the device can report back.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )
        trv = mock_self.real_trvs["climate.trv1"]
        trv.target_temp_step = 0.5
        trv.adapter = MagicMock()
        trv.adapter.set_temperature = AsyncMock(return_value=True)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 20.7,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

        trv.adapter.set_temperature.assert_awaited_once_with(
            mock_self, "climate.trv1", pytest.approx(20.5)
        )
        assert trv.last_temperature == pytest.approx(20.5)
        assert trv.echo_setpoint_values() == [pytest.approx(20.7), pytest.approx(20.5)]


# ---------------------------------------------------------------------------
# Setpoint watchdogs across a failing write
# ---------------------------------------------------------------------------


def _collect_setpoint_watchdogs(mock_self):
    """Keep the setpoint watchdogs a cycle starts and close every other task."""
    watchdogs = []

    def _create_task(coro, **kwargs):
        if kwargs.get("name", "").startswith("bt_check_target_temp_"):
            watchdogs.append(coro)
            return Mock()
        return _close_coro(coro)

    mock_self.task_manager = Mock(create_task=Mock(side_effect=_create_task))
    return watchdogs


def _watched_write(watchdog):
    """The write id and setpoint a watchdog was started for."""
    arguments = watchdog.cr_frame.f_locals
    return arguments["write_id"], arguments["setpoint"]


class TestSetpointWatchdogAcrossAFailingWrite:
    """The newest setpoint write always has a watchdog of its own."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [HomeAssistantError, ServiceValidationError])
    async def test_a_write_that_raises_is_still_watched(self, error):
        """A write whose service call raises still releases the channel.

        The cycle writes 23.0; the next writes 24.0 and the call raises, yet
        the device takes the value. The 24.0 write is watched like any other,
        so the device's report of it releases ``target_temp_received`` and
        the next cycle, finding 24.0 in place, leaves the channel open.
        """
        trv_attrs = {"temperature": 20.0}
        mock_self = _make_mock_self(trv_state=HVACMode.HEAT, trv_attrs=trv_attrs)
        trv = mock_self.real_trvs["climate.trv1"]
        watchdogs = _collect_setpoint_watchdogs(mock_self)
        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(
                _PATCHES["set_temperature"],
                autospec=True,
                side_effect=[None, error("device timed out")],
            ) as set_temperature,
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 23.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

            mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
            mock_convert.return_value = {
                "temperature": 24.0,
                "system_mode": HVACMode.HEAT,
            }
            with pytest.raises(error):
                await control_trv(mock_self, "climate.trv1")

            assert [_watched_write(watchdog) for watchdog in watchdogs][-1] == (
                trv.last_setpoint_write_id,
                24.0,
            )

            trv_attrs["temperature"] = 24.0
            for watchdog in watchdogs:
                await watchdog
            assert trv.target_temp_received is True

            mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
            await control_trv(mock_self, "climate.trv1")

        assert set_temperature.await_count == 2
        assert trv.target_temp_received is True
        assert trv.confirmed_setpoint == 24.0

    @pytest.mark.asyncio
    async def test_a_successful_write_is_watched_once(self):
        """A write that goes out starts exactly one watchdog, for its own id."""
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )
        trv = mock_self.real_trvs["climate.trv1"]
        watchdogs = _collect_setpoint_watchdogs(mock_self)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch(_PATCHES["set_temperature"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 23.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

        assert [_watched_write(watchdog) for watchdog in watchdogs] == [
            (trv.last_setpoint_write_id, 23.0)
        ]
        for watchdog in watchdogs:
            watchdog.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("max_temp", "intent", "sent", "remembered"),
        [
            pytest.param(
                30.0, 20.7, 20.5, [22.0, 20.7, 20.5], id="rounded_onto_the_grid"
            ),
            # The safety hull clamps the intent to the device maximum before
            # the write, so the intent and the value sent are one entry.
            pytest.param(
                25.0, 27.0, 25.0, [22.0, 25.0], id="clamped_to_the_device_maximum"
            ),
        ],
    )
    async def test_the_value_a_failed_write_sent_is_remembered(
        self, max_temp, intent, sent, remembered
    ):
        """The value a write sent is a known write whether or not its call raises.

        After a write of 22.0, the cycle asks for ``intent`` on a device with
        a 0.5 step and a maximum of ``max_temp``; the delegate sends ``sent``
        and the call raises. The value sent is remembered with the intent
        and watched under its own id, so a report of it after a later write
        of 21.0 is BT's own write coming back, not a knob turn.
        """
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT, trv_attrs={"temperature": 20.0}
        )
        mock_self.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
        trv = mock_self.real_trvs["climate.trv1"]
        trv.target_temp_step = 0.5
        trv.max_temp = max_temp

        async def _refuse_the_sent_value(_self, _entity_id, temperature):
            if temperature == sent:
                raise ServiceValidationError("refused")
            return True

        trv.adapter = MagicMock()
        trv.adapter.set_temperature = AsyncMock(side_effect=_refuse_the_sent_value)
        watchdogs = _collect_setpoint_watchdogs(mock_self)

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": 22.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

            mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
            mock_convert.return_value = {
                "temperature": intent,
                "system_mode": HVACMode.HEAT,
            }
            with pytest.raises(ServiceValidationError):
                await control_trv(mock_self, "climate.trv1")
            echo_after_the_failed_write = trv.echo_setpoint_values()
            watched_after_the_failed_write = _watched_write(watchdogs[-1])
            id_after_the_failed_write = trv.last_setpoint_write_id

            mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
            mock_convert.return_value = {
                "temperature": 21.0,
                "system_mode": HVACMode.HEAT,
            }
            await control_trv(mock_self, "climate.trv1")

        for watchdog in watchdogs:
            watchdog.close()
        report = resolve_inbound_setpoint(
            mock_self,
            State("climate.trv1", HVACMode.HEAT, {"temperature": sent}),
            keys=("temperature",),
            known_values=(
                trv.last_temperature,
                trv.confirmed_setpoint,
                *trv.echo_setpoint_values(),
            ),
            step=0.5,
            log_source="test",
        )

        assert echo_after_the_failed_write == pytest.approx(remembered)
        assert watched_after_the_failed_write == (
            id_after_the_failed_write,
            pytest.approx(sent),
        )
        assert report is not None
        assert report.is_echo is True


# ---------------------------------------------------------------------------
# HomematicIP write pacing
# ---------------------------------------------------------------------------


def _paced_trv(entity_id, *, homematicip):
    """Return a TRV holding 20 °C whose config marks it HomematicIP or not."""
    return Trv.from_legacy_dict(
        entity_id,
        {
            "ignore_trv_states": False,
            "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
            "min_temp": 5.0,
            "max_temp": 30.0,
            "temperature": 20.0,
            "last_temperature": 20.0,
            "last_hvac_mode": HVACMode.HEAT,
            "hvac_mode": HVACMode.HEAT,
            "advanced": {
                "calibration_mode": CalibrationMode.NO_CALIBRATION,
                "calibration": CalibrationType.TARGET_TEMP_BASED,
                "no_off_system_mode": False,
                CONF_HOMEMATICIP: homematicip,
            },
        },
    )


class TestHomematicIPWritePacing:
    """A HomematicIP head is written at its own pace, the others at theirs."""

    PLAIN = "climate.plain"
    HMIP = "climate.hmip"
    HMIP_PEER = "climate.hmip_peer"

    @pytest.fixture(autouse=True)
    def _close_unrun_retries(self):
        """Close the budget retries a test leaves without running them."""
        self._retries = []
        yield
        for coro, _name, _task in self._retries:
            coro.close()

    def _room(self, heads):
        """Return a thermostat driving ``heads`` (entity id -> homematicip).

        Only the budget retries the cycles queue are kept, for the test to
        run, each with the task handle it was given; every other task a
        cycle creates, such as a confirmation watchdog, is closed at once.
        """
        real_trvs = {
            entity_id: _paced_trv(entity_id, homematicip=flag)
            for entity_id, flag in heads.items()
        }
        mock_self = _make_mock_self(
            trv_state=HVACMode.HEAT,
            trv_attrs={"temperature": 20.0},
            real_trvs=real_trvs,
        )
        created = self._retries

        def _capture(coro, name=None, **kwargs):
            task = Mock()
            if (name or "").startswith("bt_budget_retry_"):
                created.append((coro, name, task))
            else:
                coro.close()
            return task

        mock_self.task_manager = Mock(create_task=Mock(side_effect=_capture))
        mock_self.last_user_change_monotonic = None
        return mock_self, created

    @staticmethod
    async def _cycle(mock_self, entity_ids, target, written):
        """Run one control cycle over ``entity_ids`` asking for ``target``."""

        async def _record(_self, entity_id, value):
            written.append((entity_id, value))

        with (
            patch(_PATCHES["convert_outbound_states"]) as mock_convert,
            patch(
                _PATCHES["override_set_temperature"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_temperature"], autospec=True, side_effect=_record),
            patch(
                _PATCHES["override_set_hvac_mode"], autospec=True, return_value=False
            ),
            patch(_PATCHES["set_hvac_mode"], autospec=True),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            mock_convert.return_value = {
                "temperature": target,
                "system_mode": HVACMode.HEAT,
            }
            for entity_id in entity_ids:
                await control_trv(mock_self, entity_id)

    @staticmethod
    async def _retry_delays(created, entity_id):
        """Run the budget retries queued for ``entity_id``; return their waits.

        A retry whose task was cancelled never wakes, so it is not run.
        """
        delays = []

        async def _sleep(seconds):
            delays.append(seconds)

        for coro, name, task in created:
            if name == f"bt_budget_retry_{entity_id}" and not task.cancel.called:
                with patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep)):
                    await coro
            else:
                coro.close()
        created.clear()
        return delays

    @pytest.mark.asyncio
    async def test_a_mixed_room_paces_only_the_homematicip_head(self):
        """The plain head follows the normal pace, the HomematicIP head its own.

        A new target half a minute after the last write reaches the plain
        head at once. The HomematicIP head receives it only once its own
        interval has passed: the write is deferred, not dropped, and the
        retry queued for it wakes when the interval opens.
        """
        mock_self, created = self._room({self.PLAIN: False, self.HMIP: True})
        heads = [self.PLAIN, self.HMIP]
        written = []

        await self._cycle(mock_self, heads, 22.0, written)
        assert written == [(self.PLAIN, 22.0), (self.HMIP, 22.0)]
        await self._retry_delays(created, self.HMIP)

        mock_self.clock.advance(MIN_WRITE_INTERVAL_S + 1)
        written.clear()
        await self._cycle(mock_self, heads, 23.0, written)
        assert written == [(self.PLAIN, 23.0)]
        delays = await self._retry_delays(created, self.HMIP)
        assert delays == [
            pytest.approx(HOMEMATICIP_MIN_WRITE_INTERVAL_S - MIN_WRITE_INTERVAL_S - 1)
        ]
        assert mock_self.control_queue_task.get_nowait() is mock_self

        mock_self.clock.advance(delays[0])
        written.clear()
        await self._cycle(mock_self, [self.HMIP], 23.0, written)
        assert written == [(self.HMIP, 23.0)]

    @pytest.mark.asyncio
    async def test_an_all_homematicip_room_is_paced_per_head(self):
        """Every HomematicIP head waits its own interval between writes."""
        mock_self, created = self._room({self.HMIP: True, self.HMIP_PEER: True})
        heads = [self.HMIP, self.HMIP_PEER]
        written = []

        await self._cycle(mock_self, heads, 22.0, written)
        mock_self.clock.advance(HOMEMATICIP_MIN_WRITE_INTERVAL_S - 1)
        await self._cycle(mock_self, heads, 23.0, written)
        assert written == [(self.HMIP, 22.0), (self.HMIP_PEER, 22.0)]

        mock_self.clock.advance(1)
        await self._cycle(mock_self, heads, 23.0, written)
        assert written[2:] == [(self.HMIP, 23.0), (self.HMIP_PEER, 23.0)]
        await self._retry_delays(created, "")

    @pytest.mark.asyncio
    async def test_a_user_change_reaches_the_homematicip_head_at_the_normal_pace(self):
        """The user's own change is written within the normal interval.

        A minute after the last write the user sets a new target, and the
        HomematicIP head receives it at once. The controller's next
        recomputation a minute later waits for the head's own interval again.
        """
        mock_self, created = self._room({self.HMIP: True})
        written = []

        await self._cycle(mock_self, [self.HMIP], 22.0, written)
        mock_self.clock.advance(60.0)
        mock_self.last_user_change_monotonic = mock_self.clock.monotonic()
        await self._cycle(mock_self, [self.HMIP], 23.0, written)
        assert written == [(self.HMIP, 22.0), (self.HMIP, 23.0)]
        await self._retry_delays(created, "")

        mock_self.clock.advance(60.0)
        await self._cycle(mock_self, [self.HMIP], 23.5, written)
        assert written[2:] == []
        delays = await self._retry_delays(created, self.HMIP)
        assert delays == [pytest.approx(HOMEMATICIP_MIN_WRITE_INTERVAL_S - 60.0)]

    @pytest.mark.asyncio
    async def test_a_second_user_change_waits_for_the_normal_spacing(self):
        """Two user changes ten seconds apart coalesce on the normal spacing.

        The second change is deferred, not lost: it lands once the normal
        interval after the first write has passed.
        """
        mock_self, created = self._room({self.HMIP: True})
        written = []

        await self._cycle(mock_self, [self.HMIP], 22.0, written)
        mock_self.clock.advance(60.0)
        mock_self.last_user_change_monotonic = mock_self.clock.monotonic()
        await self._cycle(mock_self, [self.HMIP], 23.0, written)
        await self._retry_delays(created, "")

        mock_self.clock.advance(10.0)
        mock_self.last_user_change_monotonic = mock_self.clock.monotonic()
        await self._cycle(mock_self, [self.HMIP], 24.0, written)
        assert written == [(self.HMIP, 22.0), (self.HMIP, 23.0)]
        delays = await self._retry_delays(created, self.HMIP)
        assert delays == [pytest.approx(MIN_WRITE_INTERVAL_S - 10.0)]

        mock_self.clock.advance(delays[0])
        await self._cycle(mock_self, [self.HMIP], 24.0, written)
        assert written[2:] == [(self.HMIP, 24.0)]

    @pytest.mark.asyncio
    async def test_a_user_change_brings_a_pending_retry_forward(self):
        """A retry queued at the HomematicIP pace yields to a user change.

        Ten seconds after the last write the controller asks for a new
        target and is deferred for the rest of the head's own interval.
        Five seconds later the user sets a target; the retry must wake once
        the normal spacing has passed, not when the controller's slot opens.
        """
        mock_self, created = self._room({self.HMIP: True})
        written = []

        await self._cycle(mock_self, [self.HMIP], 22.0, written)
        await self._retry_delays(created, "")

        mock_self.clock.advance(10.0)
        await self._cycle(mock_self, [self.HMIP], 23.0, written)
        mock_self.clock.advance(5.0)
        mock_self.last_user_change_monotonic = mock_self.clock.monotonic()
        await self._cycle(mock_self, [self.HMIP], 24.0, written)
        assert written == [(self.HMIP, 22.0)]
        tasks = [task for _coro, name, task in created if name.endswith(self.HMIP)]
        assert [task.cancel.called for task in tasks] == [True, False]

        delays = await self._retry_delays(created, self.HMIP)
        assert delays == [pytest.approx(MIN_WRITE_INTERVAL_S - 15.0)]

        mock_self.clock.advance(delays[0])
        await self._cycle(mock_self, [self.HMIP], 24.0, written)
        assert written[1:] == [(self.HMIP, 24.0)]
