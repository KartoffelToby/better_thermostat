"""Released devices hold the learners the way a learning pause does.

The requirement behind each test: while the devices are the user's no
learner takes anything from the room, and the control cycle that takes
them back still holds, so no learning interval spans the release. The
pause switch keeps its own meaning: a release never shows as a pause.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.calibration import (
    _build_mpc_v2_reid_key,
    _compute_mpc_balance,
    _compute_mpc_v2_balance,
    _compute_pid_balance,
)
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration import mpc as mpc_module
from custom_components.better_thermostat.utils.calibration.mpc import build_mpc_key
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    compute as mpc_v2_compute,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    build_pid_key,
    build_pid_loop_key,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.controlling import control_queue
from tests.unit.controlling.test_control_queue import _idle_room, _stop, _wait_until
from tests.unit.test_contact_gates_controllers import _bt, _valve_trv
from tests.unit.test_pause_learning import (
    _TARGET,
    ENTITY_ID,
    _held,
    _learned,
    _learned_mpc_state,
    _pid_bt,
    _wall_clock,
)

_CLIMATE = "custom_components.better_thermostat.climate"
_GAP_S = 600.0
"""How long the devices stay released: shorter than any stale-anchor reset."""


def _make_releasable(bt: Any) -> Any:
    """Give a stand-in what releasing and taking back its devices touches."""
    bt.device_name = "Test BT"
    bt.devices_released = False
    bt.learning_paused = False
    bt.learning_held = False
    bt.learning_pause_changes = 0
    bt.startup_running = False
    bt._heating_tracker = MagicMock()
    bt._loss_tracker = MagicMock()
    bt._initialize_arrived_trvs = AsyncMock()
    bt._external_temperature_keepalive = AsyncMock()
    bt.async_write_ha_state = MagicMock()
    if not isinstance(getattr(bt, "control_queue_task", None), asyncio.Queue):
        bt.control_queue_task = asyncio.Queue(maxsize=1)
    return bt


async def _set_released(bt: Any, released: bool) -> None:
    with patch(f"{_CLIMATE}.check_critical_entities", AsyncMock()):
        await BetterThermostat.async_set_devices_released(bt, released)


def _set_paused(bt: Any, paused: bool) -> None:
    BetterThermostat.set_learning_paused(bt, paused)


# -- The hold across control cycles ------------------------------------------


class _Loop:
    """A control loop on a one-TRV room, recording the hold of every cycle."""

    def __init__(self) -> None:
        self.bt = _make_releasable(_idle_room())
        self.bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID)}
        self.queue: asyncio.Queue[object] = self.bt.control_queue_task
        self.held_by_cycle: list[bool] = []
        self.trv_writes = asyncio.Event()
        self.trv_writes.set()

        async def run_learners() -> None:
            self.held_by_cycle.append(self.bt.learning_held)

        self.bt.calculate_heating_power = run_learners

    async def control_trv(self, _bt, _entity_id, cycle=None) -> bool:
        await self.trv_writes.wait()
        return True

    async def cycle(self) -> None:
        """Run one control cycle, the one a change requested or a new one."""
        if self.queue.empty():
            self.queue.put_nowait(self.bt)
        await asyncio.wait_for(self.queue.join(), timeout=1)


async def _run(steps: Callable[[_Loop], Any]) -> _Loop:
    loop = _Loop()
    with patch(
        "custom_components.better_thermostat.utils.controlling.control_trv",
        new=loop.control_trv,
    ):
        task = asyncio.create_task(control_queue(loop.bt))
        try:
            await steps(loop)
        finally:
            await _stop(task)
    return loop


@pytest.mark.asyncio
async def test_the_learners_hold_from_the_release_on():
    """Releasing holds the learners at once and in every cycle while released.

    The pause switch's state is not touched: a release is not a pause.
    """
    seen: list[tuple[bool, bool]] = []

    async def steps(loop: _Loop) -> None:
        await _set_released(loop.bt, True)
        seen.append((loop.bt.learning_paused, loop.bt.learning_held))
        await loop.cycle()
        await loop.cycle()

    loop = await _run(steps)

    assert seen == [(False, True)]
    assert loop.held_by_cycle == [True, True]
    assert (loop.bt.learning_paused, loop.bt.learning_held) == (False, True)


@pytest.mark.asyncio
async def test_the_cycle_that_takes_the_devices_back_still_holds():
    """The take-back cycle holds; only the cycle after it learns again."""
    held_after: list[bool] = []

    async def steps(loop: _Loop) -> None:
        await loop.cycle()
        await _set_released(loop.bt, True)
        await loop.cycle()
        await _set_released(loop.bt, False)
        held_after.append(loop.bt.learning_held)
        await loop.cycle()
        held_after.append(loop.bt.learning_held)
        await loop.cycle()

    loop = await _run(steps)

    assert held_after == [True, False]
    assert loop.held_by_cycle == [False, True, True, False]
    assert loop.bt.learning_paused is False


@pytest.mark.parametrize(
    "pause_first", [True, False], ids=["pause_first", "release_first"]
)
@pytest.mark.asyncio
async def test_the_hold_lasts_until_a_cycle_after_both_have_ended(pause_first):
    """With a pause and a release, ending one of them leaves the learners held.

    Whichever ends last is followed by one more held cycle.
    """
    held_after: list[bool] = []

    async def steps(loop: _Loop) -> None:
        _set_paused(loop.bt, True)
        await _set_released(loop.bt, True)
        await loop.cycle()
        if pause_first:
            _set_paused(loop.bt, False)
        else:
            await _set_released(loop.bt, False)
        await loop.cycle()
        held_after.append(loop.bt.learning_held)
        if pause_first:
            await _set_released(loop.bt, False)
        else:
            _set_paused(loop.bt, False)
        await loop.cycle()
        held_after.append(loop.bt.learning_held)
        await loop.cycle()

    loop = await _run(steps)

    assert held_after == [True, False]
    assert loop.held_by_cycle == [True, True, True, False]


@pytest.mark.asyncio
async def test_a_release_within_a_running_cycle_holds_the_next_cycle():
    """A release that comes and goes while a cycle writes still holds the next.

    That cycle ran its learners before the release, so it cannot end the
    hold: the cycle the take-back requests runs its learners held, and only
    the cycle after that learns again.
    """
    held_after_take_back: list[object] = []

    async def steps(loop: _Loop) -> None:
        loop.trv_writes.clear()
        loop.queue.put_nowait(loop.bt)
        await _wait_until(lambda: loop.held_by_cycle == [False])
        await _set_released(loop.bt, True)
        await _set_released(loop.bt, False)
        loop.trv_writes.set()
        await asyncio.wait_for(loop.queue.join(), timeout=1)
        held_after_take_back.append((list(loop.held_by_cycle), loop.bt.learning_held))
        await loop.cycle()

    loop = await _run(steps)

    assert held_after_take_back == [([False, True], False)]
    assert loop.held_by_cycle == [False, True, False]


# -- What the take-back cycle learns -------------------------------------------


async def _release_for_a_while(bt: Any, advance: Callable[[float], None]) -> None:
    await _set_released(bt, True)
    advance(_GAP_S)
    await _set_released(bt, False)


@pytest.mark.parametrize("released", [True, False], ids=["released", "controlled"])
@pytest.mark.asyncio
async def test_the_take_back_cycle_neither_integrates_nor_tunes_the_pid(released):
    """The PID cycle after a take-back adds nothing for the time released.

    The counterpart runs the same cycle after the same time under control,
    where it integrates the error and stores what it tuned.
    """
    bt = _make_releasable(_pid_bt(paused=False))
    loop_key = build_pid_loop_key(bt, ENTITY_ID)
    store = bt.state_mgr.state.pid
    _compute_pid_balance(bt, ENTITY_ID)
    integral = store[loop_key].pid_integral
    bucket = store.get(build_pid_key(bt, ENTITY_ID))

    if released:
        await _release_for_a_while(bt, bt.clock.advance)
    else:
        bt.clock.advance(_GAP_S)
    percent, _ = _compute_pid_balance(bt, ENTITY_ID)

    assert percent is not None and percent > 0
    learned = (store[loop_key].pid_integral, store.get(build_pid_key(bt, ENTITY_ID)))
    assert (learned == (integral, bucket)) is released
    if released:
        assert store[loop_key].last_tune_ts == bt.clock.monotonic()


@pytest.mark.parametrize("released", [True, False], ids=["released", "controlled"])
@pytest.mark.asyncio
async def test_the_take_back_cycle_adapts_no_mpc_estimate(released):
    """MPC restarts its learning interval on the take-back cycle.

    Under control the same cycle adapts the estimates over the interval.
    """
    trvs = {ENTITY_ID: _valve_trv(ENTITY_ID, CalibrationMode.MPC_CALIBRATION)}
    bt = _make_releasable(_bt(trvs, window_open=False, door_open=False))
    key = build_mpc_key(bt, ENTITY_ID)
    wall, clock = _wall_clock(mpc_module, 1_000_000.0)
    state = _learned_mpc_state()
    state.last_learn_time = wall[0]
    state.last_learn_temperature = 19.5
    state.last_time = wall[0]
    bt.state_mgr.set_mpc(key, state)
    before = _learned(state)

    def advance(seconds: float) -> None:
        wall[0] += seconds

    if released:
        await _release_for_a_while(bt, advance)
    else:
        advance(_GAP_S)
    with clock:
        output, _ = _compute_mpc_balance(bt, ENTITY_ID)

    assert output is not None
    after = bt.state_mgr.get_mpc(key)
    assert (_learned(after) == before) is released
    if released:
        assert after.last_learn_time == wall[0]


@pytest.mark.parametrize("released", [True, False], ids=["released", "controlled"])
@pytest.mark.asyncio
async def test_the_take_back_cycle_holds_mpc_v2_and_cuts_the_reid_buffer(released):
    """MPC v2 moves neither its disturbance nor its integral on the take-back cycle.

    The re-identification buffer gets the paused sample that cuts its
    segments there, so no fitted transient spans the release. Under control
    the same cycle steps both estimates and records no such sample.
    """
    trvs = {ENTITY_ID: _valve_trv(ENTITY_ID, CalibrationMode.MPC_V2_CALIBRATION)}
    bt = _make_releasable(_bt(trvs, window_open=False, door_open=False))
    bt.room_temperature = _TARGET - 0.1
    wall, clock = _wall_clock(mpc_v2_compute, 1_000_000.0)

    def advance(seconds: float) -> None:
        wall[0] += seconds
        bt.clock.advance(seconds)

    with clock:
        for _ in range(3):
            _compute_mpc_v2_balance(bt, ENTITY_ID)
            advance(300.0)
        (state,) = bt.state_mgr._mpc_v2.values()
        before = _held(state)

        if released:
            await _release_for_a_while(bt, advance)
        else:
            advance(_GAP_S)
        output, _ = _compute_mpc_v2_balance(bt, ENTITY_ID)

    assert output is not None
    assert (_held(state) == before) is released
    samples = bt.state_mgr.get_mpc_v2_reid_runtime(_build_mpc_v2_reid_key(bt)).buffer
    cut = bool(samples.samples) and samples.samples[-1].learning_paused
    assert cut is released
