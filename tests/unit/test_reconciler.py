"""Behavior tests for the reconciler and the per-TRV write budget.

Lost writes converge through the periodic reconcile tick, and
non-safety writes to one TRV keep a minimum spacing.
"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import running_kernel_state
from custom_components.better_thermostat.core.fsm.mode import ModeState
from custom_components.better_thermostat.core.snapshot import HvacMode as CoreHvacMode
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.controlling import (
    control_trv,
    reconcile_tick,
)

_CTRL = "custom_components.better_thermostat.utils.controlling"


def _make_bt(*, reported_target=21.0, commanded=21.0, trv_mode=HVACMode.HEAT):
    bt = MagicMock()
    bt.device_name = "Test BT"
    bt.clock = FakeClock()
    bt.kernel_state = running_kernel_state()
    bt.startup_running = False
    bt.in_maintenance = False
    bt.ignore_states = False
    bt.degraded_mode = False
    bt.window_open = False
    bt.call_for_heat = True
    bt.preset_mode = None
    bt.cur_temp = 20.0
    bt.cur_temp_filtered = None
    bt.temp_slope = None
    bt.tolerance = 0.0
    bt.bt_target_temp = 21.0
    bt.bt_target_cooltemp = None
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.bt_min_temp = 5.0
    bt.bt_max_temp = 30.0
    bt.outdoor_sensor = None
    bt.weather_entity = None
    bt.real_trvs = {
        "climate.trv": Trv.from_legacy_dict(
            "climate.trv",
            {"last_temperature": commanded, "min_temp": 5.0, "max_temp": 30.0},
        )
    }
    state = Mock()
    state.state = trv_mode
    state.attributes = {"temperature": reported_target}
    bt.hass.states.get.return_value = state
    bt.hass.config.units.temperature_unit = "°C"
    bt.control_queue_task = MagicMock()
    bt.control_queue_task.put_nowait = MagicMock()
    return bt


class TestReconcileTick:
    """The periodic tick queues a control cycle only on divergence."""

    @pytest.mark.asyncio
    async def test_converged_device_gets_no_cycle(self):
        """Matching reported state queues nothing."""
        bt = _make_bt(reported_target=21.0, commanded=21.0)
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_lost_setpoint_write_queues_a_cycle(self):
        """A device reporting an old setpoint triggers reconciliation."""
        bt = _make_bt(reported_target=18.0, commanded=21.0)
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_mode_divergence_queues_a_cycle(self):
        """An intent of OFF against a heating device triggers reconciliation."""
        bt = _make_bt()
        bt.bt_hvac_mode = HVACMode.OFF
        bt.kernel_state = replace(
            bt.kernel_state, mode=ModeState(hvac_mode=CoreHvacMode.OFF)
        )
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_off_flag_trv_is_not_diverged_by_off_intent(self):
        """A no_off_system_mode TRV legitimately stays in 'heat' under OFF.

        BT never switches such devices off — it writes min_temp instead —
        so an OFF intent against a 'heat' device state is convergence,
        not divergence, and must not queue a cycle every tick.
        """
        bt = _make_bt(reported_target=5.0, commanded=5.0)
        bt.bt_hvac_mode = HVACMode.OFF
        bt.kernel_state = replace(
            bt.kernel_state, mode=ModeState(hvac_mode=CoreHvacMode.OFF)
        )
        bt.real_trvs["climate.trv"].advanced = {"no_off_system_mode": True}
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_trv_without_off_mode_is_not_diverged_by_off_intent(self):
        """A TRV whose hvac_modes lack OFF can never report 'off'."""
        bt = _make_bt(reported_target=5.0, commanded=5.0)
        bt.bt_hvac_mode = HVACMode.OFF
        bt.kernel_state = replace(
            bt.kernel_state, mode=ModeState(hvac_mode=CoreHvacMode.OFF)
        )
        bt.real_trvs["climate.trv"].hvac_modes = [HVACMode.HEAT]
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_off_trv_setpoint_divergence_still_detected(self):
        """The setpoint comparison keeps covering no-off devices."""
        bt = _make_bt(reported_target=21.0, commanded=5.0)
        bt.bt_hvac_mode = HVACMode.OFF
        bt.kernel_state = replace(
            bt.kernel_state, mode=ModeState(hvac_mode=CoreHvacMode.OFF)
        )
        bt.real_trvs["climate.trv"].advanced = {"no_off_system_mode": True}
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_device_grid_snap_within_half_step_is_converged(self):
        """Snapping onto the device's coarser grid is convergence.

        A device snapping a written setpoint onto its own grid moves it
        by at most half a reported step.
        """
        bt = _make_bt(reported_target=21.5, commanded=21.3)
        bt.hass.states.get.return_value.attributes["target_temp_step"] = 0.5
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_divergence_beyond_half_step_still_detected(self):
        """More than half a step apart is a genuinely lost write."""
        bt = _make_bt(reported_target=22.0, commanded=21.3)
        bt.hass.states.get.return_value.attributes["target_temp_step"] = 0.5
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_fahrenheit_step_is_compared_as_a_delta(self):
        """A °F device's step converts as an interval, not a temperature."""
        bt = _make_bt(reported_target=70.0, commanded=21.3)
        bt.hass.config.units.temperature_unit = "°F"
        # 70 °F reports as ~21.1 °C; |21.3 - 21.1| = 0.2 K is within half
        # of a 1 °F (~0.56 K) step.
        bt.hass.states.get.return_value.attributes["target_temp_step"] = 1.0
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    def _with_states(self, bt, extra):
        """Serve the TRV state plus per-entity extra states."""
        trv_state = bt.hass.states.get.return_value

        def lookup(entity_id):
            if entity_id in extra:
                return extra[entity_id]
            return trv_state

        bt.hass.states.get.side_effect = lookup

    def _state(self, value):
        state = Mock()
        state.state = value
        state.attributes = {}
        return state

    @pytest.mark.asyncio
    async def test_lost_offset_write_queues_a_cycle(self):
        """A confirmed offset that left the commanded value reconciles."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.local_temperature_calibration_entity = "number.offset"
        trv.last_calibration = 2.0
        trv.calibration_received = True
        self._with_states(bt, {"number.offset": self._state("0.0")})
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_offset_within_half_step_is_converged(self):
        """Half a calibration step of quantization is convergence."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.local_temperature_calibration_entity = "number.offset"
        trv.last_calibration = 2.0
        trv.local_calibration_step = 0.5
        trv.calibration_received = True
        self._with_states(bt, {"number.offset": self._state("1.8")})
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_unconfirmed_offset_write_is_left_to_the_write_path(self):
        """An in-flight offset write is not the reconciler's business."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.local_temperature_calibration_entity = "number.offset"
        trv.last_calibration = 2.0
        trv.calibration_received = False
        self._with_states(bt, {"number.offset": self._state("0.0")})
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_lost_valve_write_queues_a_cycle(self):
        """A valve-position entity far off the commanded percent reconciles."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.valve_position_entity = "number.valve"
        trv.valve_position_writable = True
        trv.last_valve_percent = 80
        self._with_states(bt, {"number.valve": self._state("0")})
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

    @pytest.mark.asyncio
    async def test_valve_within_tolerance_is_converged(self):
        """Small valve deviations are the device's own business."""
        bt = _make_bt()
        trv = bt.real_trvs["climate.trv"]
        trv.valve_position_entity = "number.valve"
        trv.valve_position_writable = True
        trv.last_valve_percent = 80
        self._with_states(bt, {"number.valve": self._state("77")})
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_probe_is_not_recorded(self):
        """The periodic probe leaves no flight-recorder entry.

        It shares the observe-decide step with the control cycle but
        must not fill the recorder ring.
        """
        bt = _make_bt()
        await reconcile_tick(bt)
        bt.flight_recorder.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_during_startup_and_maintenance(self):
        """The tick is inert while startup or ignore_states is active."""
        bt = _make_bt(reported_target=18.0)
        bt.startup_running = True
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()

        bt = _make_bt(reported_target=18.0)
        bt.ignore_states = True
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_not_called()


def _close_coro(coro, name=None):
    """Close an untracked coroutine to avoid RuntimeWarning."""
    coro.close()
    return Mock()


def _control_bt():
    bt = _make_bt()
    bt._temp_lock = asyncio.Lock()
    bt.task_manager = Mock(create_task=Mock(side_effect=_close_coro))
    bt.real_trvs["climate.trv"].hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    bt.real_trvs["climate.trv"].advanced = {
        "calibration_mode": CalibrationMode.NO_CALIBRATION,
        "calibration": CalibrationType.TARGET_TEMP_BASED,
        "no_off_system_mode": False,
    }
    bt.real_trvs["climate.trv"].system_mode_received = False
    bt.real_trvs["climate.trv"].target_temp_received = False
    bt.real_trvs["climate.trv"].calibration_received = False
    return bt


async def _run_setpoint_cycle(bt, target):
    """Run one control_trv cycle that wants ``target`` written."""
    with (
        patch(f"{_CTRL}.convert_outbound_states") as conv,
        patch(f"{_CTRL}.set_temperature", new=AsyncMock()) as set_temp,
        patch(f"{_CTRL}.set_hvac_mode", new=AsyncMock()),
        patch(f"{_CTRL}.override_set_hvac_mode", new=AsyncMock(return_value=False)),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        conv.return_value = {"temperature": target, "system_mode": HVACMode.HEAT}
        await control_trv(bt, "climate.trv")
    return set_temp


class TestBudgetRetry:
    """A budget-deferred setpoint write schedules its own follow-up.

    Returning success without one loses the write: the reconciler
    compares the device against the last value actually written, which
    the device still matches.
    """

    def _capture_tasks(self, bt):
        captured = []
        bt.task_manager.create_task = Mock(
            side_effect=lambda coro, name=None: captured.append((coro, name)) or Mock()
        )
        return captured

    @pytest.mark.asyncio
    async def test_deferred_write_schedules_a_retry_cycle(self):
        """The deferred setpoint is re-requested when the budget reopens."""
        bt = _control_bt()
        captured = self._capture_tasks(bt)
        await _run_setpoint_cycle(bt, target=22.0)
        assert captured == []

        bt.clock.advance(10.0)
        set_temp = await _run_setpoint_cycle(bt, target=23.0)
        set_temp.assert_not_called()
        assert len(captured) == 1
        coro, name = captured[0]
        assert "budget_retry" in name

        with patch("asyncio.sleep", new=AsyncMock()):
            await coro
        bt.control_queue_task.put_nowait.assert_called_once()
        assert bt.real_trvs["climate.trv"].budget_retry_pending is False

    @pytest.mark.asyncio
    async def test_repeated_defers_schedule_only_one_retry(self):
        """Back-to-back defers coalesce into a single pending retry."""
        bt = _control_bt()
        captured = self._capture_tasks(bt)
        await _run_setpoint_cycle(bt, target=22.0)
        bt.clock.advance(5.0)
        await _run_setpoint_cycle(bt, target=23.0)
        bt.clock.advance(5.0)
        await _run_setpoint_cycle(bt, target=23.5)
        assert len(captured) == 1
        captured[0][0].close()


class TestWatchdogHeartbeat:
    """Every deliberate control_trv outcome refreshes the watchdog.

    The watchdog detects the silent hang. Skipping an unavailable TRV,
    deferring a write to the budget, or skipping calibration on a failed
    offset read are deliberate decisions of a live loop — without a
    heartbeat they read as a stall and produce a forced cycle (and an
    ERROR log) every reconcile tick.
    """

    @pytest.mark.asyncio
    async def test_unavailable_trv_stamps_heartbeat(self):
        """The unavailable-TRV skip still counts as a completed cycle."""
        bt = _control_bt()
        bt.hass.states.get.return_value = None
        bt.clock.advance(50.0)
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await control_trv(bt, "climate.trv")
        assert result is True
        assert bt.kernel_state.last_control_monotonic == 50.0

    async def _run_setpoint(self, bt, target):
        with (
            patch(f"{_CTRL}.convert_outbound_states") as conv,
            patch(f"{_CTRL}.set_temperature", new=AsyncMock()) as set_temp,
            patch(f"{_CTRL}.set_hvac_mode", new=AsyncMock()),
            patch(f"{_CTRL}.override_set_hvac_mode", new=AsyncMock(return_value=False)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            conv.return_value = {"temperature": target, "system_mode": HVACMode.HEAT}
            await control_trv(bt, "climate.trv")
        return set_temp

    @pytest.mark.asyncio
    async def test_budget_deferred_write_stamps_heartbeat(self):
        """A budget-deferred setpoint write still counts as a cycle."""
        bt = _control_bt()
        await self._run_setpoint(bt, target=22.0)
        bt.clock.advance(10.0)
        set_temp = await self._run_setpoint(bt, target=23.0)
        set_temp.assert_not_called()
        assert bt.kernel_state.last_control_monotonic == 10.0

    @pytest.mark.asyncio
    async def test_failed_offset_read_stamps_heartbeat(self):
        """A calibration skipped on a failed offset read still counts."""
        bt = _control_bt()
        bt.real_trvs["climate.trv"].advanced = {
            "calibration_mode": CalibrationMode.DEFAULT,
            "calibration": CalibrationType.LOCAL_BASED,
            "no_off_system_mode": False,
        }
        bt.clock.advance(70.0)
        with (
            patch(f"{_CTRL}.convert_outbound_states") as conv,
            patch(f"{_CTRL}._get_valve_control", return_value=(None, None)),
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=None)),
            patch(f"{_CTRL}.set_hvac_mode", new=AsyncMock()),
            patch(f"{_CTRL}.override_set_hvac_mode", new=AsyncMock(return_value=False)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            conv.return_value = {
                "local_temperature_calibration": 1.0,
                "system_mode": HVACMode.HEAT,
            }
            result = await control_trv(bt, "climate.trv")
        assert result is True
        assert bt.kernel_state.last_control_monotonic == 70.0


class TestWriteBudget:
    """Non-safety setpoint writes keep a minimum spacing per TRV."""

    async def _run(self, bt, target):
        with (
            patch(f"{_CTRL}.convert_outbound_states") as conv,
            patch(f"{_CTRL}.set_temperature", new=AsyncMock()) as set_temp,
            patch(f"{_CTRL}.set_hvac_mode", new=AsyncMock()),
            patch(f"{_CTRL}.override_set_hvac_mode", new=AsyncMock(return_value=False)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            conv.return_value = {"temperature": target, "system_mode": HVACMode.HEAT}
            result = await control_trv(bt, "climate.trv")
        return result, set_temp

    @pytest.mark.asyncio
    async def test_first_write_passes_and_stamps_budget(self):
        """The first write goes through and records the write time."""
        bt = _control_bt()
        bt.clock.advance(100.0)
        result, set_temp = await self._run(bt, target=22.0)
        assert result is True
        set_temp.assert_called_once()
        assert bt.real_trvs["climate.trv"].last_write_monotonic == 100.0

    @pytest.mark.asyncio
    async def test_write_within_budget_window_is_deferred(self):
        """A second non-safety write within 30 s is deferred."""
        bt = _control_bt()
        await self._run(bt, target=22.0)
        bt.clock.advance(10.0)
        result, set_temp = await self._run(bt, target=23.0)
        assert result is True
        set_temp.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_after_budget_window_passes(self):
        """Once the window has passed, the next write goes through."""
        bt = _control_bt()
        await self._run(bt, target=22.0)
        bt.clock.advance(30.0)
        _, set_temp = await self._run(bt, target=23.0)
        set_temp.assert_called_once()

    @pytest.mark.asyncio
    async def test_frost_floor_write_bypasses_budget(self):
        """A setpoint rewritten by the safety hull ignores the budget."""
        bt = _control_bt()
        await self._run(bt, target=22.0)
        bt.clock.advance(1.0)
        # 1.0 °C is below the 5.0 °C frost floor -> hull rewrites -> bypass
        _, set_temp = await self._run(bt, target=1.0)
        set_temp.assert_called_once()
        assert set_temp.call_args[0][2] == 5.0


class TestOffsetWriteBudget:
    """Calibration-offset writes keep the same per-TRV spacing."""

    def _offset_bt(self):
        bt = _control_bt()
        bt.real_trvs["climate.trv"].advanced = {
            "calibration_mode": CalibrationMode.DEFAULT,
            "calibration": CalibrationType.LOCAL_BASED,
            "no_off_system_mode": False,
        }
        bt.real_trvs["climate.trv"].calibration_received = True
        bt.real_trvs["climate.trv"].last_calibration = 0.0
        bt.real_trvs["climate.trv"].local_calibration_min = -5.0
        bt.real_trvs["climate.trv"].local_calibration_max = 5.0
        return bt

    async def _run(self, bt, offset):
        with (
            patch(f"{_CTRL}.convert_outbound_states") as conv,
            patch(f"{_CTRL}._get_valve_control", return_value=(None, None)),
            patch(f"{_CTRL}.get_current_offset", new=AsyncMock(return_value=0.0)),
            patch(f"{_CTRL}.set_offset", new=AsyncMock()) as set_off,
            patch(f"{_CTRL}.set_hvac_mode", new=AsyncMock()),
            patch(f"{_CTRL}.override_set_hvac_mode", new=AsyncMock(return_value=False)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            conv.return_value = {
                "local_temperature_calibration": offset,
                "system_mode": HVACMode.HEAT,
            }
            result = await control_trv(bt, "climate.trv")
        return result, set_off

    @pytest.mark.asyncio
    async def test_first_offset_write_passes_and_stamps_budget(self):
        """The first offset write goes through and records its time."""
        bt = self._offset_bt()
        bt.clock.advance(100.0)
        result, set_off = await self._run(bt, offset=2.0)
        assert result is True
        set_off.assert_called_once()
        trv = bt.real_trvs["climate.trv"]
        assert trv.last_offset_write_monotonic == 100.0

    @pytest.mark.asyncio
    async def test_offset_write_within_budget_window_is_skipped(self):
        """A second offset write within 30 s is skipped, not blocking."""
        bt = self._offset_bt()
        await self._run(bt, offset=2.0)
        bt.real_trvs["climate.trv"].calibration_received = True
        bt.clock.advance(10.0)
        result, set_off = await self._run(bt, offset=3.0)
        assert result is True
        set_off.assert_not_called()

    @pytest.mark.asyncio
    async def test_offset_write_after_budget_window_passes(self):
        """Once the window has passed, the next offset write goes through."""
        bt = self._offset_bt()
        await self._run(bt, offset=2.0)
        bt.real_trvs["climate.trv"].calibration_received = True
        bt.clock.advance(30.0)
        _, set_off = await self._run(bt, offset=3.0)
        set_off.assert_called_once()

    @pytest.mark.asyncio
    async def test_offset_budget_is_independent_of_the_setpoint_budget(self):
        """An offset write does not consume the setpoint channel's slot."""
        bt = self._offset_bt()
        await self._run(bt, offset=2.0)
        assert bt.real_trvs["climate.trv"].last_write_monotonic is None


class TestValveWriteBudget:
    """Direct valve writes keep the same per-TRV spacing; 0 % bypasses."""

    async def _run(self, bt, percent):
        with (
            patch(f"{_CTRL}.convert_outbound_states") as conv,
            patch(
                f"{_CTRL}._get_valve_control",
                return_value=({"valve_percent": percent}, "test"),
            ),
            patch(f"{_CTRL}.set_valve", new=AsyncMock(return_value=True)) as set_valve,
            patch(f"{_CTRL}.set_hvac_mode", new=AsyncMock()),
            patch(f"{_CTRL}.override_set_hvac_mode", new=AsyncMock(return_value=False)),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            conv.return_value = {"system_mode": HVACMode.HEAT}
            result = await control_trv(bt, "climate.trv")
        return result, set_valve

    @pytest.mark.asyncio
    async def test_first_valve_write_passes_and_stamps_budget(self):
        """The first valve write goes through and records its time."""
        bt = _control_bt()
        bt.clock.advance(100.0)
        result, set_valve = await self._run(bt, percent=50)
        assert result is True
        set_valve.assert_called_once()
        assert bt.real_trvs["climate.trv"].last_valve_write_monotonic == 100.0

    @pytest.mark.asyncio
    async def test_valve_write_within_budget_window_is_skipped(self):
        """A second valve write within 30 s is skipped."""
        bt = _control_bt()
        await self._run(bt, percent=50)
        bt.clock.advance(10.0)
        _, set_valve = await self._run(bt, percent=60)
        set_valve.assert_not_called()

    @pytest.mark.asyncio
    async def test_closing_the_valve_bypasses_the_budget(self):
        """A 0 % command (overheat-safe direction) ignores the budget."""
        bt = _control_bt()
        await self._run(bt, percent=50)
        bt.clock.advance(1.0)
        _, set_valve = await self._run(bt, percent=0)
        set_valve.assert_called_once()
        assert set_valve.call_args[0][2] == 0

    @pytest.mark.asyncio
    async def test_valve_write_after_budget_window_passes(self):
        """Once the window has passed, the next valve write goes through."""
        bt = _control_bt()
        await self._run(bt, percent=50)
        bt.clock.advance(30.0)
        _, set_valve = await self._run(bt, percent=60)
        set_valve.assert_called_once()
