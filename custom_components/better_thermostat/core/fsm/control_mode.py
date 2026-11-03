"""Control-mode region: the fail-soft ladder OPTIMAL -> SENSOR_FALLBACK -> HOLD.

The rungs:

* OPTIMAL — the room sensor delivers; the control law works as configured.
* SENSOR_FALLBACK — the room sensor is unavailable or reports no
  plausible temperature, but at least one TRV reports an internal
  temperature: after a short debounce the calibration substitutes the
  mean of the available TRV-internal temperatures for the room
  temperature. Controlling on a hot-valve
  sensor is worse than on a room sensor, but strictly better than
  controlling on a silently stale reading.
* HOLD — neither room sensor nor any TRV temperature is usable: the
  controller stops adjusting and keeps the last commanded state; the
  safety hull keeps enforcing the frost floor at the command boundary.

Transitions degrade quickly (small debounce) and recover slowly: the
ladder only climbs back up after the capability has been continuously
restored for ``up_stability_seconds`` (hysteresis against flapping sensors).

The region is not persisted across restarts: the ladder starts at
OPTIMAL and re-derives its rung from live observations within one
debounce window. A persisted rung could only pin stale degradation —
the observations it was derived from are gone after a restart. The one
exception is a room sensor that has no plausible reading when startup goes
ahead: startup puts the ladder on SENSOR_FALLBACK directly, because a
missing sensor has already been missing for longer than the debounce and
an implausible reading gives the room nothing else to start on.
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

    down_debounce_seconds: float = 120.0
    up_stability_seconds: float = 300.0


# Interval of the periodic ladder evaluation, shorter than both windows of
# ``LadderParams``. A sensor that stops reporting produces no events, so this
# tick supplies the evaluation that commits its rung, at most one interval
# after the window has elapsed.
LADDER_TICK_S = 60.0


@dataclass(frozen=True)
class PendingWindow:
    """A running debounce (deeper) or stability (shallower) window.

    ``target`` is the rung the window commits to on elapse: the
    shallowest deeper rung (downgrade) or the deepest shallower rung
    (upgrade) continuously supported since ``since``.
    """

    deeper: bool
    since: float
    target: ControlMode


@dataclass(frozen=True)
class ControlModeState:
    """State of the control-mode region."""

    mode: ControlMode = ControlMode.OPTIMAL
    unavailable_sensors: tuple[str, ...] = ()
    # Start of the current annunciated degradation, keyed to sensor
    # availability and owned solely by step(). The ladder rung is tracked
    # separately in `mode` and does not write here.
    degraded_since: float | None = None
    # Running ladder window, if any. It restarts when the observation
    # returns to the current rung or crosses to the other side of it.
    pending: PendingWindow | None = None

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
        return ControlModeState(mode=state.mode, pending=state.pending)
    return ControlModeState(
        mode=state.mode,
        unavailable_sensors=tuple(unavailable_sensors),
        degraded_since=state.degraded_since if state.degraded else now,
        pending=state.pending,
    )


def _target_rung(room_sensor_ok: bool, trv_temperature_ok: bool) -> ControlMode:
    if room_sensor_ok:
        return ControlMode.OPTIMAL
    if trv_temperature_ok:
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
    trv_temperature_ok: bool,
    now: float,
    params: LadderParams,
) -> ControlModeState:
    """Advance the ladder rung from the capability observation.

    Downgrades commit after ``down_debounce_seconds`` of sustained loss;
    upgrades commit after ``up_stability_seconds`` of sustained recovery.
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
    target = _target_rung(room_sensor_ok, trv_temperature_ok)

    if target == state.mode:
        return _with_pending(state, None)

    deeper = _depth(target) > _depth(state.mode)
    threshold_seconds = (
        params.down_debounce_seconds if deeper else params.up_stability_seconds
    )
    return _advance_window(
        state,
        target=target,
        now=now,
        threshold_seconds=threshold_seconds,
        deeper=deeper,
    )


def _toward(deeper: bool, rung: ControlMode, reference: ControlMode) -> bool:
    """Return whether ``rung`` lies beyond ``reference`` in window direction."""
    if deeper:
        return _depth(rung) > _depth(reference)
    return _depth(rung) < _depth(reference)


def _advance_window(
    state: ControlModeState,
    *,
    target: ControlMode,
    now: float,
    threshold_seconds: float,
    deeper: bool,
) -> ControlModeState:
    """Run the direction-bound commit window toward ``target``.

    The window keeps its start time while the pending rung stays on the
    same side of the current mode, tracks the rung nearest the current
    one that was continuously supported, and commits to that rung once
    the window elapses. A commit short of the instantaneous target seeds
    the follow-up window toward the remaining rung.
    """
    window = state.pending
    if (
        window is not None
        and window.deeper == deeper
        and _toward(deeper, window.target, state.mode)
    ):
        since = window.since
        commit_rung = (
            window.target if _toward(deeper, target, window.target) else target
        )
    else:
        since = now
        commit_rung = target
    if now - since >= threshold_seconds:
        committed = _with_mode(state, commit_rung)
        if commit_rung == target:
            return committed
        return _with_pending(committed, PendingWindow(deeper, now, target))
    return _with_pending(state, PendingWindow(deeper, since, commit_rung))


def start_on_rung(state: ControlModeState, mode: ControlMode) -> ControlModeState:
    """Put the ladder on ``mode`` without running a debounce window.

    For an observation that outlasted the window before the ladder was
    evaluated for the first time. Any pending window is dropped, and the
    annunciation half is kept as it is.
    """
    return _with_mode(state, mode)


def _with_mode(state: ControlModeState, mode: ControlMode) -> ControlModeState:
    """Commit a rung, clearing the pending bookkeeping.

    ``degraded_since`` belongs to the annunciation half of the region and
    is passed through untouched: :func:`step` owns it, keyed to sensor
    availability. Deriving it from the rung as well would let a rung
    commit overwrite a still-valid start time — and once cleared, the
    annunciation cannot restore it while the sensor stays away.
    """
    return ControlModeState(
        mode=mode,
        unavailable_sensors=state.unavailable_sensors,
        degraded_since=state.degraded_since,
    )


def _with_pending(
    state: ControlModeState, pending: PendingWindow | None
) -> ControlModeState:
    if state.pending == pending:
        return state
    return ControlModeState(
        mode=state.mode,
        unavailable_sensors=state.unavailable_sensors,
        degraded_since=state.degraded_since,
        pending=pending,
    )
