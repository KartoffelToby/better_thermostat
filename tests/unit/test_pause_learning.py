"""Paused learning: every learner holds what it learned and regulation goes on.

The requirement behind each test: while learning is paused no cycle changes a
learned value, the controllers keep commanding the valve with what they hold,
and after the pause learning resumes from the held values without evaluating
anything that spans the pause.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    _compute_mpc_balance,
    _compute_mpc_v2_balance,
    _compute_pid_balance,
)
from custom_components.better_thermostat.climate import BetterThermostat
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration import mpc as mpc_module
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    TrvProfile,
    build_mpc_key,
    compute_mpc,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute as mpc_v2_compute,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    build_pid_key,
    build_pid_loop_key,
    compute_pid,
)
from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.thermal_learning import (
    HeatingPowerTracker,
    HeatLossTracker,
    ema_smooth,
)
from tests.factories import ThermostatStandIn, make_state
from tests.unit.test_contact_gates_controllers import _bt, _valve_trv

ENTITY_ID = "climate.trv"
START = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)


# -- Heating power ------------------------------------------------------------


def _heating_cycle(
    tracker: HeatingPowerTracker,
    start: datetime,
    *,
    paused: bool,
    from_temperature: float = 19.0,
    target: float = 21.0,
) -> datetime:
    """Run one heating cycle: 20 minutes of heating rising 1 K, then a peak and a fall.

    Returns the time of the last reading.
    """
    now = start
    for minute in range(0, 21, 5):
        tracker.update(
            from_temperature + minute / 20.0,
            HVACAction.HEATING,
            now,
            heat_target_temperature=target,
            learning_paused=paused,
        )
        now = start + timedelta(minutes=minute + 5)
    for minutes_after, temperature in ((0, 20.1), (5, 20.2), (10, 20.0)):
        tracker.update(
            from_temperature + temperature - 19.0,
            HVACAction.IDLE,
            now + timedelta(minutes=minutes_after),
            heat_target_temperature=target,
            learning_paused=paused,
        )
    return now + timedelta(minutes=10)


def test_paused_heating_cycles_leave_the_heating_power_alone():
    """No heating cycle observed while learning is paused moves the heating power.

    Short frost-protection cycles at a low target are the case: neither the
    learned power nor the target range it is weighted by may drift.
    """
    tracker = HeatingPowerTracker(heating_power=0.05)
    now = START
    for _ in range(5):
        now = _heating_cycle(tracker, now, paused=True, target=12.0)

    assert tracker.heating_power == 0.05
    assert not tracker.stats
    assert not tracker.cycles
    assert (tracker.min_target, tracker.max_target) == (18.0, 21.0)


def test_a_heating_cycle_running_when_the_pause_starts_is_discarded():
    """A heating cycle that began before the pause and ends after it teaches nothing."""
    tracker = HeatingPowerTracker(heating_power=0.05)
    tracker.update(19.0, HVACAction.HEATING, START, heat_target_temperature=21.0)
    tracker.update(
        19.5,
        HVACAction.HEATING,
        START + timedelta(minutes=10),
        heat_target_temperature=21.0,
        learning_paused=True,
    )
    # Learning resumes while the TRV still heats: the cycle spans the pause.
    later = START + timedelta(minutes=20)
    tracker.update(20.0, HVACAction.HEATING, later, heat_target_temperature=21.0)
    for minutes_after, temperature in ((5, 20.3), (10, 20.4), (15, 20.2)):
        tracker.update(
            temperature,
            HVACAction.IDLE,
            later + timedelta(minutes=minutes_after),
            heat_target_temperature=21.0,
        )

    assert tracker.heating_power == 0.05
    assert not tracker.cycles


def test_heating_power_learning_resumes_from_the_held_value():
    """The first full cycle after the pause moves the held power by one EMA step."""
    tracker = HeatingPowerTracker(heating_power=0.05)
    now = _heating_cycle(tracker, START, paused=True)
    _heating_cycle(tracker, now + timedelta(minutes=5), paused=False)

    assert len(tracker.cycles) == 1
    cycle = tracker.cycles[0]
    stats = tracker.stats[0]
    assert cycle["start_temperature"] == 19.0
    assert tracker.heating_power == pytest.approx(
        ema_smooth(0.05, cycle["rate_kelvin_per_min"], stats["alpha"]), abs=1e-4
    )


def test_paused_idle_stretches_leave_the_heat_loss_alone():
    """An idle stretch during the pause, or one it cuts into, is not evaluated."""
    tracker = HeatLossTracker(heat_loss_rate=0.02)
    tracker.update(21.0, HVACAction.IDLE, START)
    for minute in range(10, 70, 10):
        tracker.update(
            21.0 - minute / 60.0,
            HVACAction.IDLE,
            START + timedelta(minutes=minute),
            learning_paused=True,
        )
    tracker.update(20.0, HVACAction.HEATING, START + timedelta(minutes=70))

    assert tracker.heat_loss_rate == 0.02
    assert not tracker.cycles


def test_heat_loss_learning_resumes_with_a_stretch_that_starts_after_the_pause():
    """After the pause the next idle stretch is measured from its own start."""
    tracker = HeatLossTracker(heat_loss_rate=0.02)
    tracker.update(21.0, HVACAction.IDLE, START, learning_paused=True)
    resume = START + timedelta(minutes=30)
    tracker.update(20.5, HVACAction.IDLE, resume)
    tracker.update(20.0, HVACAction.IDLE, resume + timedelta(minutes=50))
    tracker.update(20.0, HVACAction.HEATING, resume + timedelta(minutes=55))

    assert len(tracker.cycles) == 1
    assert tracker.cycles[0]["start_temperature"] == 20.5
    assert tracker.heat_loss_rate == pytest.approx(ema_smooth(0.02, 0.01, 0.1))


# -- PID ----------------------------------------------------------------------

_TARGET = 21.0
# The hot-water case: 0.6 K below target, flat, valve far from open.
_COLD_FLAT_ROOM = 20.4


def _run_pid(
    state: PIDState,
    *,
    cycles: int,
    start: float,
    room: float = _COLD_FLAT_ROOM,
    paused: bool,
    params: PIDParams | None = None,
) -> tuple[list[float], float]:
    """Run PID cycles every five minutes; return the outputs and the last time."""
    params = params or PIDParams()
    outputs = []
    now = start
    for index in range(cycles):
        now = start + index * 300.0
        percent, _debug, state = compute_pid(
            params,
            _TARGET,
            room,
            None,
            0.0,
            "key",
            state=state,
            now=now,
            learning_paused=paused,
        )
        outputs.append(percent)
    return outputs, now


def _tuned_state() -> PIDState:
    return PIDState(
        pid_integral=8.0,
        pid_kp=60.0,
        pid_ki=0.01,
        pid_kd=2000.0,
        pid_last_meas=_COLD_FLAT_ROOM,
        pid_last_time=1000.0,
        last_percent=40.0,
    )


def test_paused_pid_neither_tunes_nor_winds_up_on_a_room_that_gets_no_heat():
    """A room left cold by a boiler heating hot water teaches the PID nothing.

    Unpaused, the sluggishness rule raises Ki and Kp every five minutes and
    the integral grows; paused, both stay, and the controller still commands
    an opening from P and the held integral.
    """
    paused_state = _tuned_state()
    outputs, _ = _run_pid(paused_state, cycles=12, start=1300.0, paused=True)

    assert (paused_state.pid_kp, paused_state.pid_ki, paused_state.pid_kd) == (
        60.0,
        0.01,
        2000.0,
    )
    assert paused_state.pid_integral == 8.0
    assert all(output == round(60.0 * 0.6 + 8.0) for output in outputs)

    learning_state = _tuned_state()
    _run_pid(learning_state, cycles=12, start=1300.0, paused=False)
    assert learning_state.pid_ki is not None and learning_state.pid_ki > 0.01
    assert learning_state.pid_integral > 8.0


def test_paused_pid_holds_the_integral_through_a_sign_change():
    """The integrator relief near the target leaves a paused integral alone too."""
    state = _tuned_state()
    state.last_error_sign = 1
    _run_pid(state, cycles=1, start=1300.0, room=_TARGET + 0.05, paused=True)

    assert state.pid_integral == 8.0


def test_pid_learning_resumes_without_catching_up_on_the_pause():
    """After the pause the integral takes one cycle's step and tuning waits its interval.

    The interval runs from the last held cycle, so a cycle sooner than that
    after the pause does not tune; the first one a full interval later does.
    """
    state = _tuned_state()
    _, paused_until = _run_pid(state, cycles=12, start=1300.0, paused=True)

    _run_pid(state, cycles=1, start=paused_until + 60.0, paused=False)
    assert state.pid_integral == pytest.approx(8.0 + 0.01 * 0.6 * 60.0)
    assert (state.pid_kp, state.pid_ki) == (60.0, 0.01)

    _run_pid(state, cycles=1, start=paused_until + 300.0, paused=False)
    assert state.pid_integral == pytest.approx(8.0 + 0.01 * 0.6 * 300.0)
    assert state.pid_ki == pytest.approx(0.012)


def test_a_crossing_during_the_pause_is_no_overshoot_after_it():
    """Auto-tune compares no side of the target seen before the pause with one after it."""
    state = _tuned_state()
    _run_pid(state, cycles=2, start=1300.0, room=20.0, paused=False)
    assert state.last_delta_sign == 1
    gains = (state.pid_kp, state.pid_ki, state.pid_kd)

    _, paused_until = _run_pid(state, cycles=3, start=1900.0, room=21.5, paused=True)
    _run_pid(state, cycles=2, start=paused_until + 300.0, room=21.5, paused=False)

    assert (state.pid_kp, state.pid_ki, state.pid_kd) == gains
    assert state.last_delta_sign == -1


def _pid_bt(*, paused: bool) -> ThermostatStandIn:
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.heat_target_temperature = _TARGET
    bt.room_temperature = _COLD_FLAT_ROOM
    bt.room_temperature_filtered = None
    bt.temperature_slope = 0.0
    bt.contact_open = False
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.learning_held = paused
    bt.clock = FakeClock(monotonic_value=1300.0)
    bt.kernel_state = make_state()
    bt.real_trvs = {ENTITY_ID: Trv(entity_id=ENTITY_ID, valve_max_opening=100.0)}
    bt.real_trvs[ENTITY_ID].advanced = {}
    bt.state_mgr = MagicMock()
    bt.state_mgr.state.pid = {build_pid_loop_key(bt, ENTITY_ID): _tuned_state()}
    bt.state_mgr.set_pid.side_effect = bt.state_mgr.state.pid.__setitem__
    return bt


def test_a_paused_pid_dispatch_writes_no_learned_gains_to_the_store():
    """Across paused cycles the store's loop entry keeps its integral and no bucket learns.

    A bucket entry holds the gains learned at one target; a paused cycle must
    not create one or write the gains it ran with into one.
    """
    bt = _pid_bt(paused=True)
    loop_key = build_pid_loop_key(bt, ENTITY_ID)
    for _ in range(12):
        percent, _ = _compute_pid_balance(bt, ENTITY_ID)
        assert percent is not None and percent > 0
        bt.clock.advance(300.0)

    store = bt.state_mgr.state.pid
    assert build_pid_key(bt, ENTITY_ID) not in store
    assert store[loop_key].pid_integral == 8.0
    assert store[loop_key].last_tune_ts == 1300.0 + 11 * 300.0


def test_an_unpaused_pid_dispatch_stores_what_it_tuned():
    """The counterpart: without the pause the same cycles tune the bucket."""
    bt = _pid_bt(paused=False)
    for _ in range(12):
        _compute_pid_balance(bt, ENTITY_ID)
        bt.clock.advance(300.0)

    bucket = bt.state_mgr.state.pid[build_pid_key(bt, ENTITY_ID)]
    assert bucket.pid_ki is not None and bucket.pid_ki > 0.01


# -- MPC ----------------------------------------------------------------------


def _learned_mpc_state() -> MpcState:
    return MpcState(
        gain_est=0.05,
        loss_est=0.01,
        ka_est=0.0005,
        solar_gain_est=0.01,
        min_effective_percent=12.0,
        trv_profile=TrvProfile.THRESHOLD,
        profile_confidence=0.4,
        profile_samples=5,
        loss_learn_count=4,
        gain_learn_count=4,
        last_percent=60.0,
        last_target_temperature=_TARGET,
    )


_MPC_LEARNED_FIELDS = (
    "gain_est",
    "loss_est",
    "ka_est",
    "solar_gain_est",
    "min_effective_percent",
    "dead_zone_hits",
    "perf_curve",
    "trv_profile",
    "profile_confidence",
    "profile_samples",
    "loss_learn_count",
    "gain_learn_count",
    "recent_errors",
    "regime_boost_active",
    "consecutive_insufficient_heat",
)


def _learned(state: MpcState) -> dict[str, object]:
    snapshot = asdict(state)
    return {name: snapshot[name] for name in _MPC_LEARNED_FIELDS}


def _run_mpc(
    state: MpcState,
    *,
    cycles: int,
    start: float,
    paused: bool,
    room: float = 20.0,
    trv: float = 22.0,
) -> tuple[list[int], float]:
    """Run MPC cycles every five minutes on a room that does not warm up."""
    params = MpcParams(enable_min_effective_percent=True)
    outputs = []
    now = start
    for index in range(cycles):
        now = start + index * 300.0
        with (
            patch.object(mpc_module, "time", return_value=now),
            patch.object(mpc_module.random, "random", return_value=0.0),
        ):
            out, state = compute_mpc(
                MpcInput(
                    key="uid:climate.trv:t21.0",
                    target_temperature=_TARGET,
                    room_temperature=room,
                    trv_temperature=trv,
                    outdoor_temperature=0.0,
                    learning_paused=paused,
                ),
                params,
                state=state,
                all_states={},
            )
        assert out is not None
        outputs.append(out.valve_percent)
    return outputs, now


def test_paused_mpc_keeps_every_learned_value_and_keeps_heating():
    """A room that does not answer the valve teaches a paused MPC nothing.

    Unpaused, the same cycles move the gain estimate; paused, every learned
    value stays and the controller keeps commanding an opening.
    """
    paused_state = _learned_mpc_state()
    before = _learned(paused_state)
    outputs, _ = _run_mpc(paused_state, cycles=24, start=1_000_000.0, paused=True)

    assert _learned(paused_state) == before
    assert min(outputs) > 0

    learning_state = _learned_mpc_state()
    _run_mpc(learning_state, cycles=24, start=1_000_000.0, paused=False)
    assert _learned(learning_state) != before


def test_paused_mpc_starts_no_forced_loss_calibration_and_ends_one_running():
    """A paused MPC does not close the valve to sample the heat loss."""
    state = _learned_mpc_state()
    outputs, _ = _run_mpc(
        state, cycles=1, start=1_000_000.0, paused=True, room=_TARGET + 0.2
    )
    assert state.is_calibration_active is False

    running = replace(_learned_mpc_state(), is_calibration_active=True)
    _run_mpc(running, cycles=1, start=1_000_000.0, paused=True, room=20.5)
    assert running.is_calibration_active is False


def test_paused_mpc_derives_no_insulation_from_the_room():
    """A model without an insulation estimate does not take one from a paused room."""
    state = replace(_learned_mpc_state(), ka_est=None)
    _run_mpc(state, cycles=1, start=1_000_000.0, paused=True)

    assert state.ka_est is None


def _wall_clock(module, start: float):
    """Patch a controller module's ``time`` to a clock the test advances."""
    wall = [start]
    return wall, patch.object(module, "time", side_effect=lambda: wall[0])


def test_a_held_mpc_dispatch_restarts_the_learning_interval():
    """The dispatcher hands the hold to MPC, which restarts its interval on it."""
    trvs = {"climate.solo": _valve_trv("climate.solo", CalibrationMode.MPC_CALIBRATION)}
    bt = _bt(trvs, window_open=False, door_open=False)
    key = build_mpc_key(bt, "climate.solo")
    wall, clock = _wall_clock(mpc_module, 1_000_000.0)
    bt.state_mgr.set_mpc(
        key,
        replace(
            _learned_mpc_state(),
            last_learn_time=1_000_000.0 - 600.0,
            last_learn_temperature=19.0,
            last_time=1_000_000.0 - 300.0,
        ),
    )

    bt.learning_held = True
    with clock:
        output, _ = _compute_mpc_balance(bt, "climate.solo")

    assert output is not None
    assert bt.state_mgr.get_mpc(key).last_learn_time == wall[0]


def test_a_held_mpc_v2_dispatch_holds_the_disturbance_and_integral():
    """The dispatcher hands the hold to MPC v2, which then moves neither estimate."""
    trvs = {
        "climate.solo": _valve_trv("climate.solo", CalibrationMode.MPC_V2_CALIBRATION)
    }
    bt = _bt(trvs, window_open=False, door_open=False)
    bt.room_temperature = _TARGET - 0.1
    wall, clock = _wall_clock(mpc_v2_compute, 1_000_000.0)
    with clock:
        for _ in range(3):
            _compute_mpc_v2_balance(bt, "climate.solo")
            wall[0] += 300.0
        (state,) = bt.state_mgr._mpc_v2.values()
        before = _held(state)

        bt.learning_held = True
        for _ in range(3):
            output, _ = _compute_mpc_v2_balance(bt, "climate.solo")
            assert output is not None
            wall[0] += 300.0

    assert _held(state) == before


def test_mpc_learning_resumes_with_an_interval_that_starts_after_the_pause():
    """The first learning interval after the pause starts at its end, not before it."""
    state = _learned_mpc_state()
    _, paused_until = _run_mpc(state, cycles=6, start=1_000_000.0, paused=True)

    assert state.last_learn_time == paused_until
    assert state.last_trv_temperature_ts == paused_until
    assert state.last_room_temperature_ts == paused_until
    assert (state.u_integral, state.time_integral) == (0.0, 0.0)


# -- MPC v2 -------------------------------------------------------------------


def _run_mpc_v2(
    state: MpcV2State, *, cycles: int, start: float, paused: bool
) -> tuple[list[int], float]:
    """Run MPC v2 cycles every five minutes on a room that stays 0.1 K short.

    The valve stays well inside its range, so the integral is free to move.
    """
    outputs = []
    now = start
    for index in range(cycles):
        now = start + index * 300.0
        out, state = compute_mpc_v2(
            MpcV2Input(
                key="k",
                target_temperature=_TARGET,
                room_temperature=_TARGET - 0.1,
                outdoor_temperature=10.0,
                applied_valve_percent=outputs[-1] if outputs else None,
                learning_paused=paused,
            ),
            MpcV2Params(),
            state=state,
            now=now,
        )
        assert out is not None
        outputs.append(out.valve_percent)
    return outputs, now


def _warm_mpc_v2() -> tuple[MpcV2State, float]:
    state = MpcV2State()
    _, now = _run_mpc_v2(state, cycles=12, start=1_000_000.0, paused=False)
    return state, now


def _held(state: MpcV2State) -> tuple[float, float, float]:
    assert state.controller is not None
    controller = state.controller
    return (
        controller.dob.D_hat_K_per_min,
        controller.dob.planning_filtered,
        controller.optimiser.e_integral_K_min,
    )


def test_paused_mpc_v2_holds_its_disturbance_and_integral():
    """While paused the disturbance estimate and the error integral do not move.

    The observer still follows the room, and the controller still plans a
    valve opening from what it holds.
    """
    state, now = _warm_mpc_v2()
    before = _held(state)
    outputs, _ = _run_mpc_v2(state, cycles=12, start=now + 300.0, paused=True)

    assert _held(state) == before
    assert min(outputs) > 0
    assert state.controller is not None
    assert state.controller.kalman.x_hat[0] == pytest.approx(_TARGET - 0.1, abs=0.1)

    learning, learning_now = _warm_mpc_v2()
    _run_mpc_v2(learning, cycles=12, start=learning_now + 300.0, paused=False)
    assert _held(learning)[2] != before[2]
    assert _held(learning)[0] != before[0]


def test_mpc_v2_integral_resumes_with_one_step_after_the_pause():
    """The first plan after the pause integrates one re-plan step, not the pause."""
    state, now = _warm_mpc_v2()
    _, paused_until = _run_mpc_v2(state, cycles=12, start=now + 300.0, paused=True)
    before = _held(state)[2]
    _run_mpc_v2(state, cycles=1, start=paused_until + 300.0, paused=False)

    assert state.controller is not None
    step_minutes = state.controller.params.qp.step_s / 60.0
    assert _held(state)[2] - before == pytest.approx(-0.1 * min(5.0, step_minutes))


# -- The thermostat -----------------------------------------------------------


def _bare_thermostat() -> tuple[BetterThermostat, MagicMock]:
    """Return a thermostat shell and the mock its state is published through."""
    bt = object.__new__(BetterThermostat)
    bt.learning_paused = False
    bt.learning_held = False
    bt.control_queue_task = asyncio.Queue(maxsize=1)
    publish = MagicMock()
    bt.async_write_ha_state = publish
    bt.config = MagicMock(device_name="Test BT")
    return bt, publish


def test_learners_hold_from_the_moment_learning_is_paused():
    """Pausing holds the learners at once, before any control cycle has run."""
    bt, publish = _bare_thermostat()
    bt.set_learning_paused(True)

    assert (bt.learning_paused, bt.learning_held) == (True, True)
    publish.assert_called_once()


def test_learners_hold_until_a_control_cycle_ran_after_the_pause():
    """Resuming leaves the learners held and asks for the cycle that ends the hold."""
    bt, publish = _bare_thermostat()
    bt.set_learning_paused(True)
    bt.set_learning_paused(False)

    assert (bt.learning_paused, bt.learning_held) == (False, True)
    assert bt.control_queue_task.qsize() == 1


def test_setting_the_pause_it_already_has_changes_nothing():
    """A repeated command neither publishes nor asks for a cycle."""
    bt, publish = _bare_thermostat()
    bt.set_learning_paused(False)

    publish.assert_not_called()
    assert bt.control_queue_task.empty()
