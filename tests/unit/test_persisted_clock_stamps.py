"""Persisted controller timestamps must stay meaningful across a clock change.

The runtime state store keeps controller timestamps across restarts. A
stamp read from ``time.monotonic()`` counts seconds since the host booted:
a restart of Home Assistant alone continues it, a reboot of the host starts
it over near zero. After a reboot every monotonic stamp in the store lies in
the future of the clock it is compared against. A stamp read from the wall
clock lies in the future after the wall clock stepped back.
"""

import json
from unittest.mock import patch

from custom_components.better_thermostat.utils.calibration import (
    mpc as mpc_module,
    pid as pid_module,
)
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    compute_mpc,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    MAX_DT_S,
    PIDParams,
    PIDState,
    compute_pid,
    sanitize_pid_state,
)
from custom_components.better_thermostat.utils.state_manager import (
    RuntimeState,
    _deserialize,
    _serialize,
)

_PREVIOUS_UPTIME_S = 20 * 86400.0
"""How long the host ran before the reboot, in seconds."""

_UPTIME_AFTER_REBOOT_S = 180.0
"""The monotonic clock on the first control cycle after the reboot."""

_WALL_START_S = 1_700_000_000.0
"""The wall-clock reading of the first MPC cycle."""

_WALL_STEP_BACK_S = 4 * 3600.0
"""How far the wall clock steps back, in seconds."""

_MPC_WALL_STAMPS = (
    "last_update_ts",
    "last_time",
    "last_trv_temp_ts",
    "last_window_open_ts",
    "last_learn_time",
    "last_residual_time",
    "virtual_temp_ts",
    "last_room_temp_ts",
    "last_integration_ts",
    "created_ts",
)
"""Every MPC state field that holds a wall-clock stamp."""


# -- Monotonic stamps: restored from a longer previous uptime -----------------


def _restored_after_reboot(state: PIDState) -> PIDState:
    """Return ``state`` after a trip through the store, sanitised for use."""
    raw = json.loads(json.dumps(_serialize(RuntimeState(pid={"k": state}))))
    restored, _health = sanitize_pid_state(_deserialize(raw).pid["k"], PIDParams())
    return restored


def _cycle(params, state, *, now, room, target=21.0, slope=0.0):
    """Run one PID cycle at the monotonic reading ``now``."""
    with patch.object(pid_module, "monotonic", return_value=now):
        return compute_pid(params, target, room, room, slope, "k", state=state)


def _state_at_shutdown(**overrides) -> PIDState:
    """Return a settled PID state whose stamps come from a long uptime."""
    state = PIDState(
        pid_integral=30.0,
        pid_last_meas=20.8,
        pid_last_error=0.2,
        pid_last_time=_PREVIOUS_UPTIME_S,
        pid_kp=60.0,
        pid_ki=0.01,
        pid_kd=2000.0,
        last_tune_ts=_PREVIOUS_UPTIME_S - 60.0,
        last_percent=40.0,
        last_output_change_ts=_PREVIOUS_UPTIME_S - 60.0,
        last_target_temp=21.0,
    )
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


def test_output_follows_the_controller_on_the_first_cycle_after_a_host_reboot():
    """The first cycle after a host reboot puts out what the controller asks.

    The hold time protects the valve from rapid changes within one run; an
    output that was last changed before the reboot has been held long enough.
    """
    params = PIDParams(auto_tune=False)
    state = _restored_after_reboot(_state_at_shutdown())

    percent, debug, state = _cycle(params, state, now=_UPTIME_AFTER_REBOOT_S, room=20.8)

    assert debug["hold_time_rem"] <= params.min_hold_time_s
    assert percent == round(debug["u"])
    assert percent != 40


def test_hold_and_tuning_stamps_ahead_of_the_clock_reset_on_their_own():
    """A hold or tuning stamp from the previous uptime is dropped by itself.

    The measurement stamp can be missing from the store while the other
    two survive; each stamp that lies ahead of the clock is from the
    previous uptime whatever the others hold.
    """
    params = PIDParams(auto_tune=False)
    state = _restored_after_reboot(_state_at_shutdown(pid_last_time=0.0))

    percent, debug, state = _cycle(params, state, now=_UPTIME_AFTER_REBOOT_S, room=20.8)

    assert percent == round(debug["u"])
    assert percent != 40
    assert state.last_tune_ts == 0.0


def test_output_is_held_within_the_hold_time_after_a_core_restart():
    """Within one uptime the hold time still keeps a small change back.

    A restart of Home Assistant alone continues the monotonic clock, so an
    output changed a minute before the restart is still held.
    """
    params = PIDParams(auto_tune=False)
    state = _restored_after_reboot(_state_at_shutdown())

    percent, debug, _ = _cycle(params, state, now=_PREVIOUS_UPTIME_S + 30.0, room=20.8)

    assert percent == 40
    assert 0 < debug["hold_time_rem"] <= params.min_hold_time_s


def test_derivative_does_not_read_the_downtime_drift_as_a_one_second_change():
    """The derivative after a host reboot is no larger than after any gap.

    The room moves while the host is down, and how long it was down is not
    known after a reboot. The derivative may at most treat the move as
    spread over the longest gap it integrates, never as a change within one
    second.
    """
    params = PIDParams(auto_tune=False)
    state = _restored_after_reboot(_state_at_shutdown())
    drift_k = 0.3

    _, debug, _ = _cycle(params, state, now=_UPTIME_AFTER_REBOOT_S, room=20.8 - drift_k)

    assert abs(debug["d"]) <= params.kd * drift_k / MAX_DT_S


def test_derivative_after_a_core_restart_spreads_the_drift_over_the_gap():
    """After a restart of Home Assistant the derivative uses the real gap."""
    params = PIDParams(auto_tune=False)
    state = _restored_after_reboot(_state_at_shutdown())

    _, debug, _ = _cycle(params, state, now=_PREVIOUS_UPTIME_S + 240.0, room=20.5)

    assert debug["dt_s"] == 240.0
    assert abs(debug["d"]) <= params.kd * 0.3 / 240.0


def test_auto_tune_reads_no_overshoot_across_a_host_reboot():
    """The error before a reboot and the first one after it form no pair.

    The room was well below target at shutdown and reached it while the
    host was down. Auto-tune reads an overshoot from two errors of
    consecutive cycles; across the downtime they are not, so the first
    cycle after the reboot leaves the proportional and derivative gains.
    """
    params = PIDParams(auto_tune=True, min_hold_time_s=0.0)
    state = _restored_after_reboot(_state_at_shutdown(last_abs_error=1.0))
    first_cycle_s = params.tune_min_interval_s + 60.0

    _, _, state = _cycle(params, state, now=first_cycle_s, room=21.0)

    assert (state.pid_kp, state.pid_kd) == (60.0, 2000.0)


def test_integrator_relief_reads_no_setpoint_crossing_across_a_host_reboot():
    """A room that crossed the setpoint while the host was down keeps its integral.

    The relief answers a sign change of the error between consecutive
    cycles. The error before the reboot and the first one after it are
    not consecutive, so the first cycle integrates as from a fresh start.
    """
    params = PIDParams(auto_tune=False, min_hold_time_s=0.0)
    below_at_shutdown = _restored_after_reboot(_state_at_shutdown(last_error_sign=1))
    no_sign_at_shutdown = _restored_after_reboot(
        _state_at_shutdown(last_error_sign=None)
    )

    _, after_crossing, _ = _cycle(
        params, below_at_shutdown, now=_UPTIME_AFTER_REBOOT_S, room=21.05
    )
    _, fresh, _ = _cycle(
        params, no_sign_at_shutdown, now=_UPTIME_AFTER_REBOOT_S, room=21.05
    )

    assert after_crossing["i_relief"] is False
    assert after_crossing["i"] == fresh["i"]


def _tuning_cycles(start_s: float) -> PIDState:
    """Run a sluggish room for three cycles from ``start_s`` and return the state.

    The room sits well below target, does not move, and the valve is not yet
    fully open: the pattern auto-tune answers by raising the integral gain.
    """
    params = PIDParams(auto_tune=True, min_hold_time_s=0.0)
    state = _restored_after_reboot(_state_at_shutdown())
    for cycle in range(3):
        _, _, state = _cycle(params, state, now=start_s + cycle * 300.0, room=20.7)
    return state


def test_auto_tune_resumes_within_the_first_cycles_after_a_host_reboot():
    """Auto-tune answers a sluggish room within the first cycles after a reboot.

    The minimum interval between two tunings is measured within one run; a
    tuning before the reboot does not hold back the next one.
    """
    state = _tuning_cycles(_UPTIME_AFTER_REBOOT_S)

    assert state.pid_ki > 0.01
    assert state.last_tune_ts <= _UPTIME_AFTER_REBOOT_S + 2 * 300.0


def test_auto_tune_resumes_within_the_first_cycles_after_a_core_restart():
    """After a restart of Home Assistant auto-tune resumes as scheduled."""
    state = _tuning_cycles(_PREVIOUS_UPTIME_S + 300.0)

    assert state.pid_ki > 0.01


# -- Wall-clock stamps: a step back of the wall clock -------------------------


def _mpc_cycle(state: MpcState, wall_s: float, room: float) -> tuple[int, MpcState]:
    """Run one MPC cycle at the wall-clock reading ``wall_s``."""
    inp = MpcInput(
        key="k",
        target_temp_C=21.0,
        current_temp_C=room,
        trv_temp_C=22.0,
        temp_slope_K_per_min=0.0,
        outdoor_temp_C=5.0,
    )
    with patch.object(mpc_module, "time", return_value=wall_s):
        out, state = compute_mpc(inp, MpcParams(), state=state, all_states={})
    assert out is not None
    return out.valve_percent, state


def test_every_mpc_stamp_ahead_of_the_clock_is_taken_as_absent():
    """No MPC stamp stays ahead of the wall clock after a cycle.

    Every wall-clock stamp starts a day in the future, as after the clock
    stepped back; one cycle later none lies ahead of the clock.
    """
    ahead = _WALL_START_S + 86400.0
    state = MpcState(last_percent=100.0, last_temp=20.0, last_trv_temp=21.0)
    for name in _MPC_WALL_STAMPS:
        setattr(state, name, ahead)

    _, state = _mpc_cycle(state, _WALL_START_S, room=20.0)

    ahead_of_clock = {
        name: getattr(state, name)
        for name in _MPC_WALL_STAMPS
        if getattr(state, name) is not None and getattr(state, name) > _WALL_START_S
    }
    assert ahead_of_clock == {}


def test_mpc_backs_off_on_the_first_cycle_after_the_wall_clock_steps_back():
    """A room over target closes the valve right after a clock step back.

    Before the step the room is cold and the valve fully open. The step
    back leaves the last update ahead of the clock; the minimum interval
    between updates is measured within one clock, so it does not hold the
    valve open until the clock catches up. Loss learning resumes as the
    room cools.
    """
    state = MpcState()
    wall = _WALL_START_S
    for cycle in range(20):
        percent, state = _mpc_cycle(state, wall, room=19.0 + 0.02 * cycle)
        wall += 300.0
    assert percent == 100
    learned_before = state.loss_learn_count

    wall -= _WALL_STEP_BACK_S
    percent, state = _mpc_cycle(state, wall, room=21.8)
    assert percent < 100

    for cycle in range(1, 10):
        wall += 300.0
        _, state = _mpc_cycle(state, wall, room=21.8 - 0.02 * cycle)
    assert state.loss_learn_count > learned_before
