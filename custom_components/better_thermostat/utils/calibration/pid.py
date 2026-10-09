"""
PID controller for Better Thermostat calibration.

Goals:
- Provide a classic PID controller with conservative auto-tuning for temperature control.
- Compute valve opening percentage based on temperature error and trends.

Notes
-----
- This module only computes recommendations; writing to the device stays in adapters/controlling.
- Per-room state (EMA, hysteresis, rate limit) is owned by the caller and passed
  in explicitly; the ``StateManager`` is the single source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
import math
from time import monotonic
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

from ...core.calibrator import CalibratorHealth

if TYPE_CHECKING:
    from ...climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# Upper bound for one integration step. Control cycles nominally run every
# few minutes; 600 s covers two 5-minute cycles, so a single missed cycle
# still integrates fully while a stale ``pid_last_time`` (calibrator
# switched away and back hours later) cannot wind the integrator up in one
# giant step.
MAX_DT_S = 600.0


class PIDDebugInfo(TypedDict, total=False):
    """Debug information from PID controller."""

    mode: str
    error: str
    dt_s: float | None
    e_K: float | None
    p: float | None
    i: float | None
    d: float | None
    u: float | None
    kp: float | None
    ki: float | None
    kd: float | None
    anti_windup_blocked: bool
    i_relief: bool
    slope_in: float | None
    slope_ema: float | None
    meas_current_used: float | None
    meas_external_raw: float | None
    meas_trv_C: float | None
    meas_smooth_C: float | None
    d_meas_per_s: float | None
    hold_time_rem: int


# --- PID State -----------------------------------------------


@dataclass
class PIDState:
    """State of the PID controller of one TRV.

    The store keeps two kinds of entries. The loop entry of a TRV, keyed by
    :func:`build_pid_loop_key`, carries the integral, the measurement chain,
    the timestamps and the last output across target changes, together with
    what the user set: ``auto_tune`` and the gains set by hand. A bucket
    entry, keyed by :func:`build_pid_key` per 0.5 °C target, carries the
    gains learned at that target. A cycle runs on the loop entry with the
    gains :func:`pid_cycle_params` resolves from both.
    """

    # PID-State
    pid_integral: float = 0.0
    pid_last_meas: float | None = None
    pid_last_error: float | None = None
    pid_last_time: float = 0.0
    pid_kp: float | None = None
    pid_ki: float | None = None
    pid_kd: float | None = None
    auto_tune: bool | None = None
    # Auto-Tuning State
    last_tune_ts: float = 0.0
    last_delta_sign: int | None = None
    last_error_sign: int | None = None
    previous_abs_error: float | None = None
    last_abs_error: float | None = None
    # Smoothing
    ema_slope: float | None = None
    # Slew-rate limiter
    last_percent: float = 0.0
    # Hold-time
    last_output_change_ts: float = 0.0
    last_target_temperature: float | None = None


# --- PID Parameters -----------------------------------------------

DEFAULT_PID_KP = 60.0
DEFAULT_PID_KI = 0.01
DEFAULT_PID_KD = 2000.0
DEFAULT_PID_AUTO_TUNE = True

type PidGain = Literal["kp", "ki", "kd"]
# The range a gain may hold, set by hand through its number or loaded from the
# store. A gain outside it is a poisoned state and goes back to its default.
# Auto-tuning keeps to the narrower ranges in ``PIDParams``.
PID_GAIN_LIMITS: dict[PidGain, tuple[float, float]] = {
    "kp": (0.0, 1000.0),
    "ki": (0.0, 100.0),
    "kd": (0.0, 10000.0),
}


def pid_gain(state: PIDState, gain: PidGain) -> float | None:
    """Return the learned or user-set value of one PID gain.

    Parameters
    ----------
    state : PIDState
        the PID state holding the gain
    gain : PidGain
        which gain to read

    Returns
    -------
    float | None
        the gain, or None while the configured default applies
    """
    if gain == "kp":
        return state.pid_kp
    if gain == "ki":
        return state.pid_ki
    return state.pid_kd


def set_pid_gain(state: PIDState, gain: PidGain, value: float | None) -> None:
    """Set one PID gain; None hands it back to the configured default.

    Parameters
    ----------
    state : PIDState
        the PID state holding the gain
    gain : PidGain
        which gain to set
    value : float | None
        the new gain
    """
    if gain == "kp":
        state.pid_kp = value
    elif gain == "ki":
        state.pid_ki = value
    else:
        state.pid_kd = value


@dataclass
class PIDParams:
    """Configuration parameters for the PID computation.

    Contains all tuning options used by the PID controller.
    """

    # PID-Parameter
    kp: float = DEFAULT_PID_KP
    ki: float = DEFAULT_PID_KI
    kd: float = DEFAULT_PID_KD
    # Integrator clamp (anti-windup) in percentage points
    i_min: float = -100.0
    i_max: float = 100.0
    # Derivative on measurement
    d_on_measurement: bool = True
    d_smoothing_alpha: float = 0.5
    # Auto-Tuning
    auto_tune: bool = DEFAULT_PID_AUTO_TUNE
    tune_min_interval_s: float = 300.0
    overshoot_threshold_K: float = 0.2
    kp_min: float = 10.0
    kp_max: float = 500.0
    kp_step_mul: float = 0.9
    kp_step_mul_up: float = 1.1
    kd_min: float = 100.0
    kd_max: float = 10000.0
    kd_step_mul: float = 1.1
    ki_min: float = 0.001
    ki_max: float = 2.0
    ki_step_mul_up: float = 1.2
    ki_step_mul_down: float = 0.8
    sluggish_slope_threshold_K_min: float = 0.005
    steady_state_band_K: float = 0.1
    # Hold-time
    min_hold_time_s: float = 300.0
    big_change_threshold_percent: float = 33.0


# --- Helper Functions -----------------------------------------------


def _r(value: float | None, decimals: int = 2) -> float | None:
    """Round to decimals if not None."""
    return round(value, decimals) if value is not None else None


# --- PID Computation -----------------------------------------------


def _forget_stamps_from_a_previous_uptime(state: PIDState, now: float) -> None:
    """Drop the stored stamps when they lie ahead of this cycle's clock.

    The stamps are read from the monotonic clock, which counts from the
    host's boot and survives a restart of Home Assistant alone. After a
    host reboot the stored stamps lie ahead of it, and every interval
    measured against them comes out negative: the hold time and the tuning
    interval would not elapse until the new uptime passes the old one.
    How long the host was down is unknown, so the stamps and the
    measurements they belong to restart as on a first cycle, the error
    and its sign that auto-tune and the integrator relief compare between
    consecutive cycles among them. The integral
    and the learned gains are kept.
    """
    latest = max(state.pid_last_time, state.last_output_change_ts, state.last_tune_ts)
    if latest <= now:
        return
    state.pid_last_time = 0.0
    state.last_output_change_ts = 0.0
    state.last_tune_ts = 0.0
    state.pid_last_meas = None
    state.pid_last_error = None
    state.last_abs_error = None
    state.previous_abs_error = None
    state.last_error_sign = None


def observe_standby(
    params: PIDParams,
    state: PIDState,
    inp_room_temperature: float | None,
    now: float,
    inp_room_temperature_filtered: float | None = None,
) -> PIDState:
    """Track the measurement chain while actuation is suppressed.

    Bumpless transfer: during window-open or OFF the controller emits
    nothing, but ``pid_last_meas`` (the D-channel's smoothed measurement)
    and ``pid_last_time`` keep following the room. The first cycle after
    control resumes then sees a fresh measurement and a small ``dt`` —
    no derivative kick and no one-step integral jump computed from an
    hours-old timestamp. The integral itself stays frozen.
    """
    room_temperature = inp_room_temperature
    if inp_room_temperature_filtered is not None:
        room_temperature = inp_room_temperature_filtered
    if room_temperature is None:
        return state

    _forget_stamps_from_a_previous_uptime(state, now)
    if params.d_on_measurement:
        try:
            a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
        except TypeError, ValueError:
            a = 0.5
        prev = state.pid_last_meas
        state.pid_last_meas = (
            room_temperature
            if prev is None
            else ((1.0 - a) * prev + a * room_temperature)
        )
    else:
        state.pid_last_meas = room_temperature
    state.pid_last_time = now
    return state


def compute_pid(
    params: PIDParams,
    inp_target_temperature: float | None,
    inp_room_temperature: float | None,
    inp_trv_temperature: float | None,
    inp_temperature_slope_K_per_min: float | None,
    key: str,
    inp_room_temperature_filtered: float | None = None,
    max_opening_percent: float | None = None,
    *,
    state: PIDState,
    now: float | None = None,
) -> tuple[float, PIDDebugInfo, PIDState]:
    """Compute PID-based valve opening percentage.

    Parameters
    ----------
    params:
        PID tuning parameters.
    inp_target_temperature:
        Target temperature.
    inp_room_temperature:
        Current external temperature.
    inp_trv_temperature:
        TRV internal temperature.
    inp_temperature_slope_K_per_min:
        Temperature slope.
    key:
        Unique key for state storage.
    inp_room_temperature_filtered:
        Optional EMA-filtered external temperature for learning.
    max_opening_percent:
        Optional maximum valve opening percentage.
    state:
        Mutable controller state, owned by the caller (typically read from
        and written back to the ``StateManager``).  It is mutated in place
        and returned.
    now:
        Monotonic timestamp of this cycle; defaults to ``time.monotonic()``.
        Callers with an injected clock pass their own reading so the
        controller shares the entity's time source.

    Returns
    -------
    tuple[float, PIDDebugInfo, PIDState]
        ``(percent_open, debug_info, updated_state)``.

    The integration step ``dt`` derived from ``now - pid_last_time`` is
    clamped to :data:`MAX_DT_S` so a stale timestamp cannot produce one
    oversized integral step.
    """
    if now is None:
        now = monotonic()

    st = state
    _forget_stamps_from_a_previous_uptime(st, now)

    max_opening = 100.0
    if isinstance(max_opening_percent, (int, float)):
        max_opening = max(0.0, min(100.0, float(max_opening_percent)))

    _LOGGER.debug(
        "better_thermostat PID: input for %s: target=%.1f current=%.1f trv=%.1f slope=%.3f kp=%.1f ki=%.3f kd=%.1f",
        key,
        inp_target_temperature or 0.0,
        inp_room_temperature or 0.0,
        inp_trv_temperature or 0.0,
        inp_temperature_slope_K_per_min or 0.0,
        st.pid_kp or 0.0,
        st.pid_ki or 0.0,
        st.pid_kd or 0.0,
    )

    # Determine effective current temperature (prefer EMA)
    room_temperature = inp_room_temperature
    if inp_room_temperature_filtered is not None:
        room_temperature = inp_room_temperature_filtered

    # Delta T
    if inp_target_temperature is None or room_temperature is None:
        # Without temperatures we can only keep the previous value
        percent = 0.0
        pid_dbg: PIDDebugInfo = {"mode": "pid", "error": "no_temps"}
        return percent, pid_dbg, st

    delta_kelvin = inp_target_temperature - room_temperature
    e = delta_kelvin

    # Update previous_abs_error before setting current
    st.previous_abs_error = st.last_abs_error
    st.last_abs_error = abs(delta_kelvin)

    # Time difference, bounded to [1.0, MAX_DT_S] seconds. A stale
    # pid_last_time (calibrator switched away and back hours later) would
    # otherwise produce a huge dt and wind the integrator up in one step.
    dt = now - st.pid_last_time if st.pid_last_time > 0 else 0.0
    if dt <= 0 or dt < 1.0:
        dt = 1.0
    dt = min(dt, MAX_DT_S)

    # Initialize the learned gains once from the passed-in params
    if st.pid_kp is None:
        st.pid_kp = params.kp
    if st.pid_ki is None:
        st.pid_ki = params.ki
    if st.pid_kd is None:
        st.pid_kd = params.kd

    # Remove duplicate integrator update - only use conditional anti-windup below

    # Derivative
    d_term = 0.0
    p_term: float | None = None
    i_term: float | None = None
    u: float | None = None
    meas_now: float | None = None
    smoothed: float | None = None
    d_meas: float | None = None

    if params.d_on_measurement:
        # Use effective current temperature (EMA) for derivative
        meas_now = room_temperature
        # EMA smoothing for the D channel only
        try:
            a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
        except TypeError, ValueError:
            a = 0.5
        prev = st.pid_last_meas
        smoothed = meas_now if prev is None else ((1.0 - a) * prev + a * meas_now)
        if prev is not None:
            d_meas = (smoothed - prev) / dt
            d_term = -float(st.pid_kd) * d_meas
        # Stored (smoothed) measurement is updated after the u calculation below
    # Derivative on error: use the previous cycle's stored error so a setpoint
    # change produces a derivative kick. This is what distinguishes the mode
    # from derivative-on-measurement above, where the setpoint term cancels.
    elif st.pid_last_error is not None:
        d_err = (e - st.pid_last_error) / dt
        d_term = float(st.pid_kd) * d_err

    # Update the slope EMA in PID mode too (for logging/diagnostics)
    try:
        s_in = inp_temperature_slope_K_per_min
        if s_in is not None:
            if st.ema_slope is None:
                st.ema_slope = s_in
            else:
                st.ema_slope = 0.6 * st.ema_slope + 0.4 * s_in
    except TypeError:
        _LOGGER.debug(
            "better_thermostat PID: slope EMA update skipped for %s", key, exc_info=True
        )

    # Proportional term
    p_term = float(st.pid_kp) * e

    # Conditional anti-windup: only integrate when not saturated
    aw_blocked = False
    i_relief = False
    i_prev = st.pid_integral
    # Proposed integrator update (tentative)
    i_prop = i_prev + float(st.pid_ki) * e * dt
    # Clamp
    i_prop = max(params.i_min, min(params.i_max, i_prop))
    # Tentative control output before checking saturation
    u_prop = p_term + i_prop + d_term
    # Saturated control output
    u_sat = max(0.0, min(max_opening, u_prop))
    # If saturated and the error would worsen saturation, block integration
    if (u_prop > u_sat and e > 0) or (u_prop < u_sat and e < 0):
        i_term = i_prev
        aw_blocked = True
    else:
        i_term = i_prop

    # Integrator relief near setpoint: when the error changes sign and we are
    # within the near band, reduce the integrator slightly so the valve opens
    # or closes earlier.
    try:
        cur_sign = 1 if e > 0 else (-1 if e < 0 else 0)
        if (
            st.last_error_sign is not None
            and st.last_error_sign != 0
            and cur_sign not in (0, st.last_error_sign)
            and abs(delta_kelvin or 0.0) <= params.steady_state_band_K
        ):
            decay = 0.8  # 20% relief
            i_term *= decay
            i_relief = True
    except TypeError:
        _LOGGER.debug(
            "better_thermostat PID: integrator relief skipped for %s",
            key,
            exc_info=True,
        )

    # Final control output
    u = p_term + i_term + d_term  # PID
    # Commit the integrator state when integration was not anti-windup blocked.
    # Integrator relief only moves the value toward zero (de-saturating), so a
    # relief adjustment must still be persisted even when anti-windup blocked
    # this cycle's growth; otherwise the relief is applied to the output but
    # silently forgotten for the next cycle.
    if not aw_blocked or i_relief:
        st.pid_integral = i_term

    # --- Slew-Rate & Hold-Time Logic ---
    # 1. Calculate raw desired change (unlimited)
    percent_unlimited = max(0.0, min(max_opening, u))
    raw_change = percent_unlimited - st.last_percent

    # 2. Check for Big Change (Bypass filters)
    is_big_change = abs(raw_change) >= params.big_change_threshold_percent

    # 3. Check Target Change
    target_changed = False
    if (
        st.last_target_temperature is not None
        and abs(inp_target_temperature - st.last_target_temperature) > 0.05
    ):
        target_changed = True
    st.last_target_temperature = inp_target_temperature

    # 4. Hold-Time Check
    time_since_change = now - st.last_output_change_ts
    blocked_by_hold = False

    if (
        not target_changed
        and not is_big_change
        and time_since_change < params.min_hold_time_s
        and st.last_output_change_ts > 0
    ):
        blocked_by_hold = True

    if blocked_by_hold:
        percent = st.last_percent
    else:
        # 5. No Slew Rate - apply calculated value directly
        percent = percent_unlimited

        # Update timestamp if value changed significantly or it's the first run
        if abs(percent - st.last_percent) >= 0.1 or st.last_output_change_ts == 0:
            st.last_output_change_ts = now

    # Clamp final result
    percent = max(0.0, min(100.0, percent))
    # Round to nearest integer to avoid micro-updates that trigger TRV logic
    percent = round(percent)

    # Update last_percent
    st.last_percent = percent

    # Update PID state (store the measurement for the D term)
    if params.d_on_measurement:
        base = room_temperature
        try:
            a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
        except TypeError, ValueError:
            a = 0.5
        prev = st.pid_last_meas
        st.pid_last_meas = base if prev is None else ((1.0 - a) * prev + a * base)
    else:
        st.pid_last_meas = room_temperature
    # Refresh the last error together with pid_last_time on every cycle,
    # regardless of the derivative mode. Otherwise a switch back to
    # derivative-on-error would pair a stale error with a fresh timestamp and
    # compute a spurious derivative spike.
    st.pid_last_error = e
    st.pid_last_time = now

    # Remember the error sign for the next cycle
    st.last_error_sign = 1 if e > 0 else (-1 if e < 0 else 0)

    # Optional auto-tuning (conservative)
    if params.auto_tune:
        _auto_tune_pid(
            params,
            st,
            percent,
            delta_kelvin,
            inp_temperature_slope_K_per_min or 0.0,
            now,
        )

    # Store debug values
    try:
        # Basic debug info (also for graphs)
        pid_dbg = {
            "mode": "pid",
            "dt_s": _r(dt, 2),
            "e_K": _r(e, 2),
            "p": _r(p_term, 2),
            "i": _r(i_term, 2),
            "d": _r(d_term, 2),
            "u": _r(u, 2),
            "kp": float(st.pid_kp) if st.pid_kp is not None else None,
            "ki": float(st.pid_ki) if st.pid_ki is not None else None,
            "kd": float(st.pid_kd) if st.pid_kd is not None else None,
            # Anti-windup indicator
            "anti_windup_blocked": aw_blocked,
            "i_relief": i_relief,
            # Slope (input and EMA)
            "slope_in": _r(inp_temperature_slope_K_per_min, 3),
            "slope_ema": _r(st.ema_slope, 3),
            # Measurements
            "meas_current_used": _r(room_temperature, 2),
            "meas_external_raw": _r(inp_room_temperature, 2),
            "meas_trv_C": _r(inp_trv_temperature, 2),
            "meas_smooth_C": _r(smoothed, 2),
            "d_meas_per_s": _r(d_meas, 4),
            "hold_time_rem": (
                int(max(0, params.min_hold_time_s - (now - st.last_output_change_ts)))
                if st.last_output_change_ts > 0
                else 0
            ),
        }
    except TypeError, ValueError, OverflowError:
        pid_dbg = {"mode": "pid", "error": "debug_failed"}

    _LOGGER.debug(
        "better_thermostat PID: output for %s: percent=%.1f%%, p_term=%.2f, i_term=%.2f, d_term=%.2f, integral=%.2f",
        key,
        percent,
        p_term or 0.0,
        i_term or 0.0,
        d_term,
        st.pid_integral,
    )

    return percent, pid_dbg, st


def _auto_tune_pid(
    params: PIDParams,
    st: PIDState,
    percent: float,
    delta_kelvin: float,
    slope: float,
    now_ts: float,
) -> None:
    """Very conservative auto-tuning based on simple heuristics.

    Goals:
    - On frequent overshoot (ΔT changes sign, peak > overshoot_threshold): lower kp a bit, raise kd a bit.
    - On sluggishness (ΔT > band_near and slope very small): raise ki a bit (moderately).
    - In quasi-steady state (|ΔT| < steady_state_band and small percent): lower ki a bit to avoid drift.
    - Minimum interval between adjustments (tune_min_interval_s), clamp the gains within limits.
    """
    try:
        # Minimum interval
        if (now_ts - st.last_tune_ts) < params.tune_min_interval_s:
            return
        sign = 1 if delta_kelvin > 0 else (-1 if delta_kelvin < 0 else 0)
        overshoot = False
        # Harden overshoot detection: only when previous abs(error) > band and new abs(error) < band
        if (
            st.previous_abs_error is not None
            and st.previous_abs_error > params.steady_state_band_K
            and abs(delta_kelvin) < params.steady_state_band_K
        ):
            overshoot = True
        st.last_delta_sign = sign if sign != 0 else st.last_delta_sign

        tuned = False
        kp = params.kp if st.pid_kp is None else float(st.pid_kp)
        ki = params.ki if st.pid_ki is None else float(st.pid_ki)
        kd = params.kd if st.pid_kd is None else float(st.pid_kd)

        # 1) Overshoot: kp slightly down, kd slightly up, ki slightly down
        if overshoot:
            kp = max(params.kp_min, kp * params.kp_step_mul)
            kd = min(params.kd_max, kd * params.kd_step_mul)
            ki = max(params.ki_min, ki * params.ki_step_mul_down)
            tuned = True

        # 2) Sluggishness: ΔT clearly > band_near, but slope very small -> Ki up, Kp up
        # Use EMA slope if available for more stable tuning
        check_slope = st.ema_slope if st.ema_slope is not None else slope
        if (
            delta_kelvin > params.steady_state_band_K
            and abs(check_slope) < params.sluggish_slope_threshold_K_min
            and percent < 95.0
        ):
            ki = min(params.ki_max, max(params.ki_min, ki * params.ki_step_mul_up))
            kp = min(params.kp_max, max(params.kp_min, kp * params.kp_step_mul_up))
            tuned = True

        # 3) Quasi-steady state: |ΔT| < steady_state_band and small control output -> Ki slightly down
        if abs(delta_kelvin) < params.steady_state_band_K and percent < 20.0:
            ki = max(params.ki_min, min(params.ki_max, ki * params.ki_step_mul_down))
            tuned = True

        if tuned:
            st.pid_kp = kp
            st.pid_ki = ki
            st.pid_kd = kd
            st.last_tune_ts = now_ts
    except ValueError, TypeError:
        # Best-effort: numerische Probleme ignorieren
        return


def sanitize_pid_state(
    state: PIDState, params: PIDParams
) -> tuple[PIDState, CalibratorHealth]:
    """Self-heal a (possibly poisoned) PID state before computing.

    Non-finite values are dropped back to defaults, runaway gains return
    to the configured defaults, and a wound-up integrator is reset. The
    returned health grade reports the worst pathology found.
    """
    health = CalibratorHealth.HEALTHY

    def _finite(value: float | None) -> bool:
        return value is None or math.isfinite(value)

    if not _finite(state.pid_integral):
        state.pid_integral = 0.0
        health = CalibratorHealth.NON_FINITE
    if not _finite(state.pid_last_meas):
        state.pid_last_meas = None
        health = CalibratorHealth.NON_FINITE
    if not _finite(state.pid_last_error):
        state.pid_last_error = None
        health = CalibratorHealth.NON_FINITE
    for name in PID_GAIN_LIMITS:
        if not _finite(pid_gain(state, name)):
            set_pid_gain(state, name, None)
            health = CalibratorHealth.NON_FINITE

    runaway = any(
        (gain := pid_gain(state, name)) is not None and not low <= gain <= high
        for name, (low, high) in PID_GAIN_LIMITS.items()
    )
    if runaway:
        state.pid_kp = None
        state.pid_ki = None
        state.pid_kd = None
        if health == CalibratorHealth.HEALTHY:
            health = CalibratorHealth.RUNAWAY_GAINS

    windup = not params.i_min <= state.pid_integral <= params.i_max
    if windup:
        state.pid_integral = 0.0
        if health == CalibratorHealth.HEALTHY:
            health = CalibratorHealth.WINDUP_SUSPECT

    return state, health


# --- Key Builder Helper -----------------------------------------------


class _HasUniqueId(Protocol):
    """Structural type for objects keyed by a Home Assistant ``unique_id``."""

    @property
    def unique_id(self) -> str | None: ...


def resolve_unique_id(obj: _HasUniqueId) -> str:
    """Return the id used to key per-entity persistent state.

    An entity without a unique id keys its state under ``"bt"``, so every
    site keys state the same way.
    """
    return obj.unique_id or "bt"


def build_pid_loop_key(self: _HasUniqueId, entity_id: str) -> str:
    """Build the key of a TRV's PID loop entry: ``{unique_id}:{entity_id}``.

    It carries no target, so the entry follows the TRV across target
    changes. The bucket keys of :func:`build_pid_key` extend it.
    """
    return f"{resolve_unique_id(self)}:{entity_id}"


def round_to_bucket(temperature: float) -> float:
    """Round a target temperature to its 0.5 °C bucket centre."""
    return round(float(temperature) * 2.0) / 2.0


def format_bucket(bucket: float) -> str:
    """Format a bucket centre as a ``t<temperature>`` tag (e.g. ``t21.0``)."""
    return f"t{bucket:.1f}"


def build_pid_key(self: BetterThermostat, entity_id: str) -> str:
    """Build consistent PID state key across all modules.

    Format: {unique_id}:{entity_id}:t{target_temp:.1f}
    where target_temp is rounded to 0.5°C buckets.

    Args:
        self: BetterThermostat instance with unique_id and heat_target_temperature
        entity_id: TRV entity ID

    Returns
    -------
        PID key string
    """
    try:
        tcur = self.heat_target_temperature
        bucket_tag = (
            format_bucket(round_to_bucket(tcur))
            if isinstance(tcur, (int, float))
            else "tunknown"
        )
    except ValueError, OverflowError:
        bucket_tag = "tunknown"

    return f"{build_pid_loop_key(self, entity_id)}:{bucket_tag}"


# --- Loop and bucket entries -----------------------------------------------

_DEFAULT_PID_GAINS: dict[PidGain, float] = {
    "kp": DEFAULT_PID_KP,
    "ki": DEFAULT_PID_KI,
    "kd": DEFAULT_PID_KD,
}


def pid_loop_state(states: Mapping[str, PIDState], loop_key: str) -> PIDState:
    """Return the loop entry stored under ``loop_key``, or a new one.

    A store without a loop entry for the TRV may still hold bucket entries
    that each carried a full controller state. The new loop entry then
    continues from the bucket that ran last, so the first cycle keeps its
    integral and output. Its ``auto_tune`` is the flag the switch wrote to
    the buckets. With auto-tuning off, the gains of the latest bucket that
    holds the flag become the gains set by hand; a bucket created after the
    switch was turned off ran with defaults and does not count. The caller
    stores the returned entry.
    """
    held = states.get(loop_key)
    if held is not None:
        return held
    prefix = f"{loop_key}:"
    buckets = [state for key, state in states.items() if key.startswith(prefix)]
    if not buckets:
        return PIDState()
    latest = max(buckets, key=lambda state: state.pid_last_time)
    flagged = [state for state in buckets if state.auto_tune is not None]
    if not flagged:
        return replace(latest, pid_kp=None, pid_ki=None, pid_kd=None)
    auto_tune = flagged[0].auto_tune
    if auto_tune:
        return replace(latest, pid_kp=None, pid_ki=None, pid_kd=None, auto_tune=True)
    fixed = max(flagged, key=lambda state: state.pid_last_time)
    return replace(
        latest,
        pid_kp=fixed.pid_kp,
        pid_ki=fixed.pid_ki,
        pid_kd=fixed.pid_kd,
        auto_tune=False,
    )


def pid_auto_tune(loop: PIDState | None, bucket: PIDState | None) -> bool:
    """Return whether auto-tuning runs for a TRV.

    The switch sets the flag on the loop entry. A bucket's own flag is
    read only while the loop entry has none.
    """
    for state in (loop, bucket):
        if state is not None and state.auto_tune is not None:
            return state.auto_tune
    return DEFAULT_PID_AUTO_TUNE


def effective_pid_gain(
    loop: PIDState | None, bucket: PIDState | None, gain: PidGain
) -> float:
    """Return the gain the controller uses at the bucket's target.

    With auto-tuning off, a gain set by hand (on the loop entry) applies at
    every target. With it on, the gain learned at the target (on the bucket
    entry) comes first, and a gain set by hand is where a target not tuned
    yet starts from. Without either, the default applies.
    """
    manual = pid_gain(loop, gain) if loop is not None else None
    learned = pid_gain(bucket, gain) if bucket is not None else None
    if pid_auto_tune(loop, bucket):
        candidates = (learned, manual)
    else:
        candidates = (manual, learned)
    for value in candidates:
        if value is not None:
            return value
    return _DEFAULT_PID_GAINS[gain]


def pid_cycle_params(loop: PIDState, bucket: PIDState | None) -> PIDParams:
    """Return the parameters one cycle runs with: the gains and auto-tuning in use."""
    return PIDParams(
        kp=effective_pid_gain(loop, bucket, "kp"),
        ki=effective_pid_gain(loop, bucket, "ki"),
        kd=effective_pid_gain(loop, bucket, "kd"),
        auto_tune=pid_auto_tune(loop, bucket),
    )


def pid_cycle_state(loop: PIDState, params: PIDParams) -> PIDState:
    """Return the state one cycle runs on: the loop entry with the gains in use."""
    return replace(loop, pid_kp=params.kp, pid_ki=params.ki, pid_kd=params.kd)


def settle_pid_cycle(
    loop: PIDState, bucket: PIDState, cycle: PIDState, params: PIDParams
) -> PIDState:
    """Split a finished cycle's state back into the two entries.

    Returns the new loop entry: everything the cycle moved, with the gains
    set by hand kept. With auto-tuning on, ``bucket`` takes the gains the
    cycle ran with and tuned; with it off, the bucket keeps its own.
    """
    if params.auto_tune:
        bucket.pid_kp = cycle.pid_kp
        bucket.pid_ki = cycle.pid_ki
        bucket.pid_kd = cycle.pid_kd
    return replace(
        cycle,
        pid_kp=loop.pid_kp,
        pid_ki=loop.pid_ki,
        pid_kd=loop.pid_kd,
        auto_tune=loop.auto_tune,
    )
