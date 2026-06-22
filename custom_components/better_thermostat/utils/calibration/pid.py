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

from dataclasses import dataclass
import logging
import math
from time import monotonic
from typing import Protocol, TypedDict

_LOGGER = logging.getLogger(__name__)


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
    """State for PID controller per room."""

    # PID-State
    pid_integral: float = 0.0
    pid_last_meas: float | None = None
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
    last_target_temp: float | None = None


# --- PID Parameters -----------------------------------------------

DEFAULT_PID_KP = 60.0
DEFAULT_PID_KI = 0.01
DEFAULT_PID_KD = 2000.0
DEFAULT_PID_AUTO_TUNE = True


@dataclass
class PIDParams:
    """Configuration parameters for the PID computation.

    Contains all tuning options used by the PID controller.
    """

    # PID-Parameter
    kp: float = DEFAULT_PID_KP
    ki: float = DEFAULT_PID_KI
    kd: float = DEFAULT_PID_KD
    # Integrator-Klammer (Anti-Windup) in %-Punkten
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
    big_change_threshold_pct: float = 33.0


# --- Helper Functions -----------------------------------------------


def _r(val: float | None, decimals: int = 2) -> float | None:
    """Round to decimals if not None."""
    return round(val, decimals) if val is not None else None


# --- PID Computation -----------------------------------------------


def compute_pid(
    params: PIDParams,
    inp_target_temp_C: float | None,
    inp_current_temp_C: float | None,
    inp_trv_temp_C: float | None,
    inp_temp_slope_K_per_min: float | None,
    key: str,
    inp_current_temp_ema_C: float | None = None,
    max_opening_pct: float | None = None,
    *,
    state: PIDState,
) -> tuple[float, PIDDebugInfo, PIDState]:
    """Compute PID-based valve opening percentage.

    Parameters
    ----------
    params:
        PID tuning parameters.
    inp_target_temp_C:
        Target temperature.
    inp_current_temp_C:
        Current external temperature.
    inp_trv_temp_C:
        TRV internal temperature.
    inp_temp_slope_K_per_min:
        Temperature slope.
    key:
        Unique key for state storage.
    inp_current_temp_ema_C:
        Optional EMA-filtered external temperature for learning.
    max_opening_pct:
        Optional maximum valve opening percentage.
    state:
        Mutable controller state, owned by the caller (typically read from
        and written back to the ``StateManager``).  It is mutated in place
        and returned.

    Returns
    -------
    tuple[float, PIDDebugInfo, PIDState]
        ``(percent_open, debug_info, updated_state)``.
    """
    now = monotonic()

    st = state

    st, pathology = sanitize_pid_state(st, params)
    if pathology is not None:
        _LOGGER.warning(
            "better_thermostat: healed poisoned PID state for %s (%s)", key, pathology
        )

    max_opening = 100.0
    if isinstance(max_opening_pct, (int, float)):
        max_opening = max(0.0, min(100.0, float(max_opening_pct)))

    _LOGGER.debug(
        "better_thermostat PID: input for %s: target=%.1f current=%.1f trv=%.1f slope=%.3f kp=%.1f ki=%.3f kd=%.1f",
        key,
        inp_target_temp_C or 0.0,
        inp_current_temp_C or 0.0,
        inp_trv_temp_C or 0.0,
        inp_temp_slope_K_per_min or 0.0,
        st.pid_kp or 0.0,
        st.pid_ki or 0.0,
        st.pid_kd or 0.0,
    )

    # Determine effective current temperature (prefer EMA)
    current_temp = inp_current_temp_C
    if inp_current_temp_ema_C is not None:
        current_temp = inp_current_temp_ema_C

    # Delta T
    if inp_target_temp_C is None or current_temp is None:
        # Without temperatures we can only keep the previous value
        percent = 0.0
        pid_dbg: PIDDebugInfo = {"mode": "pid", "error": "no_temps"}
        return percent, pid_dbg, st

    delta_T = inp_target_temp_C - current_temp
    e = delta_T

    # Update previous_abs_error before setting current
    st.previous_abs_error = st.last_abs_error
    st.last_abs_error = abs(delta_T)

    # Zeitdifferenz
    dt = now - st.pid_last_time if st.pid_last_time > 0 else 0.0
    # Fix dt handling: if dt <= 0 or dt < 1.0, treat as 1.0
    if dt <= 0 or dt < 1.0:
        dt = 1.0

    # Initialisiere lernende Gains (einmalig) mit übergebenen Params
    if st.pid_kp is None:
        st.pid_kp = params.kp
    if st.pid_ki is None:
        st.pid_ki = params.ki
    if st.pid_kd is None:
        st.pid_kd = params.kd

    # Remove duplicate integrator update - only use conditional anti-windup below

    # Ableitung
    d_term = 0.0
    p_term: float | None = None
    i_term: float | None = None
    u: float | None = None
    meas_now: float | None = None
    smoothed: float | None = None
    d_meas: float | None = None

    if params.d_on_measurement:
        if dt > 0:
            # Use effective current temperature (EMA) for derivative
            meas_now = current_temp
            if meas_now is not None:
                # EMA-Glättung nur für den D-Kanal
                try:
                    a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
                except TypeError, ValueError:
                    a = 0.5
                prev = st.pid_last_meas
                smoothed = (
                    meas_now if prev is None else ((1.0 - a) * prev + a * meas_now)
                )
                if prev is not None:
                    d_meas = (smoothed - prev) / dt
                    d_term = -float(st.pid_kd) * d_meas
                # Update des gespeicherten (geglätteten) Messwerts erfolgt nach u-Berechnung unten
    # Derivative on error (benötigt letzten Fehler – approximiert über letzten Messwert)
    elif dt > 0 and st.pid_last_meas is not None:
        last_e = inp_target_temp_C - st.pid_last_meas
        d_err = (e - last_e) / dt
        d_term = float(st.pid_kd) * d_err

    # Aktualisiere die Slope-EMA auch im PID-Modus (für Logging/Diagnose)
    try:
        s_in = inp_temp_slope_K_per_min
        if s_in is not None:
            if st.ema_slope is None:
                st.ema_slope = s_in
            else:
                st.ema_slope = 0.6 * st.ema_slope + 0.4 * s_in
    except Exception:
        pass

    # Proportionalterm
    p_term = float(st.pid_kp) * e

    # Konditionales Anti-Windup: nur integrieren, wenn nicht gesättigt
    aw_blocked = False
    i_relief = False
    i_prev = st.pid_integral
    i_prop = i_prev
    if dt > 0:
        # Vorschlag für Integrator-Update (vorläufig)
        i_prop = i_prev + float(st.pid_ki) * e * dt
        # Klammern
        i_prop = max(params.i_min, min(params.i_max, i_prop))
        # Vorläufige Stellgröße ohne Sättigung prüfen
        u_prop = p_term + i_prop + d_term
        # Gesättigte Stellgröße
        u_sat = max(0.0, min(max_opening, u_prop))
        # Falls gesättigt und Fehler die Sättigung verstärken würde → Integration blockieren
        if (u_prop > u_sat and e > 0) or (u_prop < u_sat and e < 0):
            i_term = i_prev
            aw_blocked = True
        else:
            i_term = i_prop
    else:
        i_term = i_prev

    # Integrator-Entlastung nahe Soll: Wenn sich das Vorzeichen des Fehlers ändert
    # und wir innerhalb der near-Band sind, reduziere den Integrator leicht,
    # damit früher geöffnet/geschlossen wird.
    try:
        cur_sign = 1 if e > 0 else (-1 if e < 0 else 0)
        if (
            st.last_error_sign is not None
            and cur_sign not in (0, st.last_error_sign)
            and abs(delta_T or 0.0) <= params.steady_state_band_K
        ):
            decay = 0.8  # 20% Entlastung
            i_term *= decay
            i_relief = True
    except Exception:
        pass

    # Endgültige Stellgröße
    u = p_term + i_term + d_term  # PID
    # Integrator-Zustand nur übernehmen, wenn nicht blockiert
    if not aw_blocked:
        st.pid_integral = i_term

    # --- Slew-Rate & Hold-Time Logic ---
    # 1. Calculate raw desired change (unlimited)
    percent_unlimited = max(0.0, min(max_opening, u))
    raw_change = percent_unlimited - st.last_percent

    # 2. Check for Big Change (Bypass filters)
    is_big_change = abs(raw_change) >= params.big_change_threshold_pct

    # 3. Check Target Change
    target_changed = False
    if inp_target_temp_C is not None:
        if (
            st.last_target_temp is not None
            and abs(inp_target_temp_C - st.last_target_temp) > 0.05
        ):
            target_changed = True
        st.last_target_temp = inp_target_temp_C

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
    percent = int(round(percent))

    # Update last_percent
    st.last_percent = percent

    # PID-States aktualisieren (für D-Anteil Messwert speichern)
    if params.d_on_measurement:
        base = current_temp
        try:
            a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
        except TypeError, ValueError:
            a = 0.5
        if base is not None:
            prev = st.pid_last_meas
            st.pid_last_meas = base if prev is None else ((1.0 - a) * prev + a * base)
    else:
        st.pid_last_meas = current_temp
    st.pid_last_time = now

    # Fehler-Vorzeichen für nächsten Zyklus merken
    try:
        st.last_error_sign = 1 if e > 0 else (-1 if e < 0 else 0)
    except Exception:
        pass

    # Optionales Auto-Tuning (konservativ)
    if params.auto_tune:
        _auto_tune_pid(
            params, st, percent, delta_T, inp_temp_slope_K_per_min or 0.0, now
        )

    # Debug-Werte ablegen
    try:
        # Basale Debug-Infos (auch für Graphen)
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
            # Anti-Windup-Indikator
            "anti_windup_blocked": aw_blocked,
            "i_relief": i_relief,
            # Slope (Input und EMA)
            "slope_in": _r(inp_temp_slope_K_per_min, 3),
            "slope_ema": _r(st.ema_slope, 3),
            # Messwerte
            "meas_current_used": _r(current_temp, 2),
            "meas_external_raw": _r(inp_current_temp_C, 2),
            "meas_trv_C": _r(inp_trv_temp_C, 2),
            "meas_smooth_C": _r(smoothed, 2),
            "d_meas_per_s": _r(d_meas, 4),
            "hold_time_rem": (
                int(max(0, params.min_hold_time_s - (now - st.last_output_change_ts)))
                if st.last_output_change_ts > 0
                else 0
            ),
        }
    except Exception:
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
    delta_T: float | None,
    slope: float,
    now_ts: float,
) -> None:
    """Sehr konservatives Auto-Tuning basierend auf einfachen Heuristiken.

    Ziele:
    - Bei häufigem Overshoot (ΔT wechselt Vorzeichen, Peak > overshoot_threshold) → kp etwas runter, kd etwas rauf.
    - Bei Trägheit (ΔT > band_near und Slope sehr klein) → ki etwas rauf (nur moderat).
    - Im quasi-stationären Zustand (|ΔT| < steady_state_band und Prozent klein) → ki etwas runter zur Drift-Vermeidung.
    - Mindestabstand zwischen Anpassungen (tune_min_interval_s), Clamp der Gains in Grenzen.
    """
    try:
        if delta_T is None:
            return
        # Mindestabstand
        if (now_ts - st.last_tune_ts) < params.tune_min_interval_s:
            return
        sign = 1 if delta_T > 0 else (-1 if delta_T < 0 else 0)
        overshoot = False
        # Harden overshoot detection: only when previous abs(error) > band and new abs(error) < band
        if (
            st.previous_abs_error is not None
            and st.previous_abs_error > params.steady_state_band_K
            and abs(delta_T) < params.steady_state_band_K
        ):
            overshoot = True
        st.last_delta_sign = sign if sign != 0 else st.last_delta_sign

        tuned = False
        kp = float(st.pid_kp or params.kp)
        ki = float(st.pid_ki or params.ki)
        kd = float(st.pid_kd or params.kd)

        # 1) Overshoot → kp leicht runter, kd leicht rauf, ki leicht runter
        if overshoot:
            kp = max(params.kp_min, kp * params.kp_step_mul)
            kd = min(params.kd_max, kd * params.kd_step_mul)
            ki = max(params.ki_min, ki * params.ki_step_mul_down)
            tuned = True

        # 2) Trägheit: ΔT deutlich > band_near, aber Slope sehr klein -> Ki rauf, Kp rauf
        # Use EMA slope if available for more stable tuning
        check_slope = st.ema_slope if st.ema_slope is not None else slope
        if (
            delta_T > params.steady_state_band_K
            and abs(check_slope) < params.sluggish_slope_threshold_K_min
            and percent < 95.0
        ):
            ki = min(params.ki_max, max(params.ki_min, ki * params.ki_step_mul_up))
            kp = min(params.kp_max, max(params.kp_min, kp * params.kp_step_mul_up))
            tuned = True

        # 3) Quasi stationär: |ΔT| < steady_state_band und geringe Stellgröße → Ki leicht runter
        if abs(delta_T) < params.steady_state_band_K and percent < 20.0:
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
) -> tuple[PIDState, str | None]:
    """Heal a (possibly poisoned) PID state before computing.

    Non-finite values fall back to their defaults, runaway gains return
    to the configured defaults, and a wound-up integrator is reset. All
    pathologies are healed in one pass; the returned pathology names the
    most severe finding, or None.
    """
    pathology: str | None = None

    def _finite(value: float | None) -> bool:
        return value is None or math.isfinite(value)

    if not _finite(state.pid_integral):
        state.pid_integral = 0.0
        pathology = "non-finite state"
    if not _finite(state.pid_last_meas):
        state.pid_last_meas = None
        pathology = "non-finite state"
    for gain_attr in ("pid_kp", "pid_ki", "pid_kd"):
        if not _finite(getattr(state, gain_attr)):
            setattr(state, gain_attr, None)
            pathology = "non-finite state"

    runaway = (
        (
            state.pid_kp is not None
            and not params.kp_min <= state.pid_kp <= params.kp_max
        )
        or (
            state.pid_ki is not None
            and not params.ki_min <= state.pid_ki <= params.ki_max
        )
        or (
            state.pid_kd is not None
            and not params.kd_min <= state.pid_kd <= params.kd_max
        )
    )
    if runaway:
        state.pid_kp = None
        state.pid_ki = None
        state.pid_kd = None
        pathology = pathology or "runaway gains"

    if not (params.i_min <= state.pid_integral <= params.i_max):
        state.pid_integral = 0.0
        pathology = pathology or "integrator windup"

    return state, pathology


# --- Key Builder Helper -----------------------------------------------


class _HasUniqueId(Protocol):
    """Structural type for objects keyed by a Home Assistant ``unique_id``."""

    @property
    def unique_id(self) -> str | None: ...


def resolve_unique_id(obj: _HasUniqueId) -> str:
    """Return the id used to key per-entity persistent state.

    Prefers the public ``unique_id`` property, falls back to ``_unique_id`` and
    finally ``"bt"``, so every site keys state the same way.
    """
    return getattr(obj, "unique_id", None) or getattr(obj, "_unique_id", None) or "bt"


def round_to_bucket(temp: float) -> float:
    """Round a target temperature to its 0.5 °C bucket centre."""
    return round(float(temp) * 2.0) / 2.0


def format_bucket(bucket: float) -> str:
    """Format a bucket centre as a ``t<temp>`` tag (e.g. ``t21.0``)."""
    return f"t{bucket:.1f}"


def build_pid_key(self, entity_id: str) -> str:
    """Build consistent PID state key across all modules.

    Format: {unique_id}:{entity_id}:t{target_temp:.1f}
    where target_temp is rounded to 0.5°C buckets.

    Args:
        self: BetterThermostat instance with unique_id and bt_target_temp
        entity_id: TRV entity ID

    Returns
    -------
        PID key string
    """
    try:
        tcur = self.bt_target_temp
        bucket_tag = (
            format_bucket(round_to_bucket(tcur))
            if isinstance(tcur, (int, float))
            else "tunknown"
        )
    except Exception:
        bucket_tag = "tunknown"

    return f"{resolve_unique_id(self)}:{entity_id}:{bucket_tag}"
