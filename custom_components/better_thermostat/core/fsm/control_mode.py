"""Control-mode region: the fail-soft ladder OPTIMAL -> SENSOR_FALLBACK -> HOLD.

The rungs:

* OPTIMAL — the room sensor delivers; the control law works as configured.
* SENSOR_FALLBACK — the room sensor is unavailable but at least one TRV
  reports an internal temperature: after a short debounce the
  calibration substitutes the mean of the available TRV-internal
  temperatures for the room temperature. Controlling on a hot-valve
  sensor is worse than on a room sensor, but strictly better than
  controlling on a silently stale reading.
* HOLD — neither room sensor nor any TRV temperature is usable: the
  controller stops adjusting and keeps the last commanded state; the
  safety hull keeps enforcing the frost floor at the command boundary.

Transitions degrade quickly (small debounce) and recover slowly: the
ladder only climbs back up after the capability has been continuously
restored for ``up_stability_s`` (hysteresis against flapping sensors).

The region is not persisted across restarts: the ladder starts at
OPTIMAL and re-derives its rung from live observations within one
debounce window. A persisted rung could only pin stale degradation —
the observations it was derived from are gone after a restart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ControlMode(StrEnum):
    """Discrete rungs of the degradation ladder."""

    OPTIMAL = "optimal"
    SENSOR_FALLBACK = "sensor_fallback"
    HOLD = "hold"


@dataclass(frozen=True)
class LadderParams:
    """Timing of the ladder transitions in seconds."""

    down_debounce_s: float = 120.0
    up_stability_s: float = 300.0


@dataclass(frozen=True)
class ControlModeState:
    """State of the control-mode region."""

    mode: ControlMode = ControlMode.OPTIMAL
    unavailable_sensors: tuple[str, ...] = ()
    degraded_since: float | None = None
    # Pending downgrade (capability lost, debounce running).
    down_pending_since: float | None = None
    # Pending upgrade (capability restored, stability window running).
    up_pending_since: float | None = None
    # Rung the running debounce/stability window commits to on elapse:
    # the shallowest deeper rung (downgrade) or deepest shallower rung
    # (upgrade) continuously supported since the window started. The
    # window restarts when the observation returns to the current rung
    # or crosses to the other side of it.
    pending_target: ControlMode | None = None

    @property
    def degraded(self) -> bool:
        """True while any optional sensor is unavailable."""
        return bool(self.unavailable_sensors)


def step(
    state: ControlModeState, unavailable_sensors: list[str], now: float
) -> ControlModeState:
    """Record the watcher's availability check (annunciation bookkeeping).

    Any unavailable optional sensor is annunciated as degradation. The
    ladder rung itself is advanced by :func:`step_ladder` from the
    control-law-relevant capabilities.
    """
    if not unavailable_sensors:
        return ControlModeState(
            mode=state.mode,
            down_pending_since=state.down_pending_since,
            up_pending_since=state.up_pending_since,
            pending_target=state.pending_target,
        )
    return ControlModeState(
        mode=state.mode,
        unavailable_sensors=tuple(unavailable_sensors),
        degraded_since=state.degraded_since if state.degraded else now,
        down_pending_since=state.down_pending_since,
        up_pending_since=state.up_pending_since,
        pending_target=state.pending_target,
    )


def _target_rung(room_sensor_ok: bool, trv_temp_ok: bool) -> ControlMode:
    if room_sensor_ok:
        return ControlMode.OPTIMAL
    if trv_temp_ok:
        return ControlMode.SENSOR_FALLBACK
    return ControlMode.HOLD


_RUNG_ORDER = (ControlMode.OPTIMAL, ControlMode.SENSOR_FALLBACK, ControlMode.HOLD)


def _depth(mode: ControlMode) -> int:
    """Return the degradation depth of a rung (OPTIMAL shallowest)."""
    return _RUNG_ORDER.index(mode)


def step_ladder(
    state: ControlModeState,
    *,
    room_sensor_ok: bool,
    trv_temp_ok: bool,
    now: float,
    params: LadderParams,
) -> ControlModeState:
    """Advance the ladder rung from the capability observation.

    Downgrades commit after ``down_debounce_s`` of sustained loss;
    upgrades commit after ``up_stability_s`` of sustained recovery.
    The window is bound to its direction, not to one exact rung: it
    keeps running as long as the observation stays on the same side of
    the current rung (deeper while degrading, shallower while
    recovering) and commits to the rung nearest the current one that
    was continuously supported for the full window — the shallowest
    deeper rung while degrading, the deepest shallower rung while
    recovering. An observation back at the current rung restarts the
    bookkeeping from scratch; a rung beyond the committed one must earn
    its own full window afterwards.
    """
    target = _target_rung(room_sensor_ok, trv_temp_ok)

    if target == state.mode:
        return _with_pending(state, down=None, up=None, target=None)

    pending = state.pending_target
    if _depth(target) > _depth(state.mode):
        if (
            state.down_pending_since is not None
            and pending is not None
            and _depth(pending) > _depth(state.mode)
        ):
            since = state.down_pending_since
            commit_rung = pending if _depth(pending) < _depth(target) else target
        else:
            since = now
            commit_rung = target
        if now - since >= params.down_debounce_s:
            committed = _with_mode(state, commit_rung, now)
            if commit_rung == target:
                return committed
            return _with_pending(committed, down=now, up=None, target=target)
        return _with_pending(state, down=since, up=None, target=commit_rung)

    if (
        state.up_pending_since is not None
        and pending is not None
        and _depth(pending) < _depth(state.mode)
    ):
        since = state.up_pending_since
        commit_rung = pending if _depth(pending) > _depth(target) else target
    else:
        since = now
        commit_rung = target
    if now - since >= params.up_stability_s:
        committed = _with_mode(state, commit_rung, now)
        if commit_rung == target:
            return committed
        return _with_pending(committed, down=None, up=now, target=target)
    return _with_pending(state, down=None, up=since, target=commit_rung)


def _with_mode(
    state: ControlModeState, mode: ControlMode, now: float
) -> ControlModeState:
    return ControlModeState(
        mode=mode,
        unavailable_sensors=state.unavailable_sensors,
        degraded_since=(
            state.degraded_since
            if state.degraded_since is not None and mode != ControlMode.OPTIMAL
            else (now if mode != ControlMode.OPTIMAL else None)
        ),
    )


def _with_pending(
    state: ControlModeState,
    down: float | None,
    up: float | None,
    target: ControlMode | None,
) -> ControlModeState:
    if (
        state.down_pending_since == down
        and state.up_pending_since == up
        and state.pending_target == target
    ):
        return state
    return ControlModeState(
        mode=state.mode,
        unavailable_sensors=state.unavailable_sensors,
        degraded_since=state.degraded_since,
        down_pending_since=down,
        up_pending_since=up,
        pending_target=target,
    )
