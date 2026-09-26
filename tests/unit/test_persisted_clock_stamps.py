"""Persisted controller timestamps must stay meaningful across a host reboot.

The runtime state store keeps controller timestamps across restarts. A
stamp read from ``time.monotonic()`` counts seconds since the host booted:
a restart of Home Assistant alone continues it, a reboot of the host starts
it over near zero. After a reboot every monotonic stamp in the store lies in
the future of the clock it is compared against. A stamp read from the wall
clock is unaffected.

``CLOCK_OF_PERSISTED_STAMP`` classifies every timestamp field reachable from
``RuntimeState``; the completeness test fails when a stored stamp is added
without a classification. Each classified stamp then has a behavioural test:
the monotonic ones are restored from a longer previous uptime, the wall
ones are pinned to the clock they are read from.
"""

from dataclasses import fields, is_dataclass
import json
import re
from typing import get_args, get_type_hints
from unittest.mock import patch

import pytest

from custom_components.better_thermostat.utils.calibration import mpc as mpc_module
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    compute_mpc,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    compute as mpc_v2_compute,
    compute_mpc_v2,
    export_mpc_v2_state,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    ControllerSnapshot,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    MAX_DT_S,
    PIDParams,
    PIDState,
    compute_pid,
    sanitize_pid_state,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    compute_tpi,
)
from custom_components.better_thermostat.utils.state_manager import (
    FilterState,
    MpcV2ReidData,
    MpcV2StateData,
    RuntimeState,
    ThermalStats,
    _deserialize,
    _serialize,
)

MONOTONIC = "monotonic"
WALL = "wall"

CLOCK_OF_PERSISTED_STAMP = {
    (PIDState, "pid_last_time"): MONOTONIC,
    (PIDState, "last_tune_ts"): MONOTONIC,
    (PIDState, "last_output_change_ts"): MONOTONIC,
    (TpiState, "last_update_ts"): MONOTONIC,
    (MpcState, "last_update_ts"): WALL,
    (MpcState, "last_time"): WALL,
    (MpcState, "last_trv_temp_ts"): WALL,
    (MpcState, "last_window_open_ts"): WALL,
    (MpcState, "last_learn_time"): WALL,
    (MpcState, "last_residual_time"): WALL,
    (MpcState, "virtual_temp_ts"): WALL,
    (MpcState, "last_room_temp_ts"): WALL,
    (MpcState, "last_integration_ts"): WALL,
    (MpcState, "created_ts"): WALL,
    (MpcV2StateData, "last_compute_ts"): WALL,
    (MpcV2StateData, "created_ts"): WALL,
    (ControllerSnapshot, "last_t_s"): WALL,
    (ControllerSnapshot, "next_mpc_t_s"): WALL,
    (ControllerSnapshot, "last_mpc_t_s"): WALL,
    (MpcV2ReidData, "fitted_ts"): WALL,
}
"""The clock every persisted timestamp is read from, per state class and field.

``MpcV2StateData.snapshot`` stores a ``ControllerSnapshot`` as a mapping, so
the snapshot's fields are persisted too.
"""

_STAMP_NAME = re.compile(r"(_ts|_time|_t_s)$")

_PREVIOUS_UPTIME_S = 20 * 86400.0
"""How long the host ran before the reboot, in seconds."""

_UPTIME_AFTER_REBOOT_S = 180.0
"""The monotonic clock on the first control cycle after the reboot."""


def _persisted_state_classes() -> set[type]:
    """Return every dataclass the store persists, walking ``RuntimeState``."""
    found: set[type] = set()
    for hint in get_type_hints(RuntimeState).values():
        for candidate in (hint, *get_args(hint)):
            if is_dataclass(candidate):
                found.add(candidate)
    if MpcV2StateData in found:
        found.add(ControllerSnapshot)
    return found


def test_every_persisted_state_class_is_known():
    """The scan below reaches every state class the store persists."""
    assert _persisted_state_classes() == {
        PIDState,
        TpiState,
        MpcState,
        MpcV2StateData,
        ControllerSnapshot,
        MpcV2ReidData,
        ThermalStats,
        FilterState,
    }


def test_every_persisted_stamp_is_classified_by_clock():
    """Every stored timestamp names the clock it is read from."""
    stamps = {
        (cls, f.name)
        for cls in _persisted_state_classes()
        for f in fields(cls)
        if _STAMP_NAME.search(f.name)
    }
    assert stamps == set(CLOCK_OF_PERSISTED_STAMP)


# -- Monotonic stamps: restored from a longer previous uptime -----------------


def _restored_after_reboot(state: PIDState) -> PIDState:
    """Return ``state`` after a trip through the store, sanitised for use."""
    raw = json.loads(json.dumps(_serialize(RuntimeState(pid={"k": state}))))
    restored, _health = sanitize_pid_state(_deserialize(raw).pid["k"], PIDParams())
    return restored


def _cycle(params, state, *, now, room, target=21.0, slope=0.0):
    """Run one PID cycle the way the calibration runs it."""
    return compute_pid(params, target, room, room, slope, "k", state=state, now=now)


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


@pytest.mark.xfail(
    strict=True,
    reason="a hold stamp from the previous uptime lies in the future after a "
    "host reboot, so the hold gate keeps the output at the stored percent",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="a measurement stamp from the previous uptime lies in the future "
    "after a host reboot, and the derivative divides the drift over the "
    "downtime by one second",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="a tuning stamp from the previous uptime lies in the future after a "
    "host reboot, so the tuning interval never elapses",
)
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


def test_tpi_duty_cycle_ignores_its_stamp_from_the_previous_uptime():
    """TPI after a host reboot puts out what it puts out from a fresh state.

    Its stored stamp records the last update and gates nothing.
    """
    inp = TpiInput(key="k", current_temp_C=20.0, target_temp_C=21.0)

    def restored(stamp: float) -> TpiState:
        raw = _serialize(
            RuntimeState(tpi={"k": TpiState(last_percent=10.0, last_update_ts=stamp)})
        )
        return _deserialize(json.loads(json.dumps(raw))).tpi["k"]

    after_reboot, state = compute_tpi(
        inp, TpiParams(), state=restored(_PREVIOUS_UPTIME_S), now=_UPTIME_AFTER_REBOOT_S
    )
    fresh, _ = compute_tpi(
        inp, TpiParams(), state=restored(0.0), now=_UPTIME_AFTER_REBOOT_S
    )

    assert after_reboot.duty_cycle_pct == fresh.duty_cycle_pct != 10.0
    assert state.last_update_ts == _UPTIME_AFTER_REBOOT_S


# -- Wall-clock stamps: pinned to the clock they are read from ----------------

_WALL_START_S = 1_800_000_000.0
"""A wall-clock reading far from any monotonic reading."""


def _wall_fields(cls: type) -> list[str]:
    """Return the fields of ``cls`` classified as wall-clock stamps."""
    return [
        name
        for (owner, name), clock in CLOCK_OF_PERSISTED_STAMP.items()
        if owner is cls and clock == WALL
    ]


def test_mpc_stamps_are_read_from_the_wall_clock():
    """Every MPC stamp comes from the wall clock, which a reboot continues.

    The run opens a window once and moves the room and the TRV, so that
    every stamp the controller keeps is written at least once.
    """
    state = MpcState()
    wall = [_WALL_START_S]
    readings: set[float] = set()
    with patch.object(mpc_module, "time", side_effect=lambda: wall[0]):
        for cycle in range(40):
            wall[0] = _WALL_START_S + cycle * 300.0
            readings.add(wall[0])
            inp = MpcInput(
                key="k",
                target_temp_C=21.0,
                current_temp_C=19.0 + cycle * 0.01,
                trv_temp_C=22.0 + cycle * 0.05,
                temp_slope_K_per_min=0.01,
                window_open=cycle == 3,
                outdoor_temp_C=5.0,
            )
            _, state = compute_mpc(inp, MpcParams(), state=state, all_states={})

    from_wall_clock = {
        name: getattr(state, name) in readings for name in _wall_fields(MpcState)
    }
    assert from_wall_clock == dict.fromkeys(_wall_fields(MpcState), True)


def test_mpc_v2_stamps_are_read_from_the_wall_clock():
    """Every MPC v2 stamp, snapshot included, comes from the wall clock."""
    inp = MpcV2Input(
        key="k",
        target_temp_C=22.0,
        current_temp_C=19.0,
        outdoor_temp_C=5.0,
        heating_allowed=True,
        window_open=False,
    )
    with patch.object(mpc_v2_compute, "time", return_value=_WALL_START_S):
        _, state = compute_mpc_v2(inp, MpcV2Params(), None)
    payload = export_mpc_v2_state(state)
    assert payload is not None

    assert {
        name: payload[name] for name in _wall_fields(MpcV2StateData)
    } == dict.fromkeys(_wall_fields(MpcV2StateData), _WALL_START_S)
    # The next solve is scheduled one controller step after this one.
    snapshot = payload["snapshot"]
    for name in _wall_fields(ControllerSnapshot):
        assert _WALL_START_S <= snapshot[name] <= _WALL_START_S + 3600.0, name
