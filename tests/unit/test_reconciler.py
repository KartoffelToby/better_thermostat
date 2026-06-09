"""Behavior tests for the reconciler and the per-TRV write budget (M6).

This is deliberately new behavior: lost writes converge through the
periodic reconcile tick, and non-safety writes to one TRV keep a
minimum spacing.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import KernelState
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
    bt.kernel_state = KernelState()
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
        await reconcile_tick(bt)
        bt.control_queue_task.put_nowait.assert_called_once()

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


def _control_bt():
    bt = _make_bt()
    bt._temp_lock = asyncio.Lock()
    bt.task_manager = Mock(create_task=Mock())
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
