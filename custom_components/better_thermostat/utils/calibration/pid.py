"""
PID controller for Better Thermostat calibration.

Goals:
- Provide a classic PID controller with conservative auto-tuning for temperature control.
- Compute valve opening percentage based on temperature error and trends.

Notes
-----
- This module only computes recommendations; writing to the device stays in adapters/controlling.
- Lightweight per-room state by a `key` (e.g., entity_id): EMA, hysteresis, rate limit.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any

_LOGGER = logging.getLogger(__name__)


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


# --- PID Parameters -----------------------------------------------

DEFAULT_PID_KP = 20.0
DEFAULT_PID_KI = 0.02
DEFAULT_PID_KD = 400.0
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
    i_min: float = -60.0
    i_max: float = 60.0
    # Derivative on measurement
    d_on_measurement: bool = True
    trend_mix_trv: float = 0.7
    d_smoothing_alpha: float = 0.5
    # Valve slew-rate limiter (% per update)
    slew_rate: float = 5.0
    # Auto-Tuning
    auto_tune: bool = DEFAULT_PID_AUTO_TUNE
    tune_min_interval_s: float = 300.0
    overshoot_threshold_K: float = 0.3
    kp_min: float = 10.0
    kp_max: float = 500.0
    kp_step_mul: float = 0.9
    kd_min: float = 100.0
    kd_max: float = 10000.0
    kd_step_mul: float = 1.1
    ki_min: float = 0.001
    ki_max: float = 1.0
    ki_step_mul_up: float = 1.2
    ki_step_mul_down: float = 0.8
    sluggish_slope_threshold_K_min: float = 0.005
    steady_state_band_K: float = 0.1


# --- Global State Storage -----------------------------------------------

_PID_STATES: dict[str, PIDState] = {}


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
) -> tuple[float, dict[str, Any]]:
    """Compute PID-based valve opening percentage.

    Args:
        params: PID parameters
        inp_target_temp_C: Target temperature
        inp_current_temp_C: Current external temperature
        inp_trv_temp_C: TRV internal temperature
        inp_temp_slope_K_per_min: Temperature slope
        key: Unique key for state storage

    Returns
    -------
        Tuple of (percent_open, debug_info)
    """
    now = monotonic()
    st = _PID_STATES.setdefault(key, PIDState())

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

    # Delta T
    if inp_target_temp_C is None or inp_current_temp_C is None:
        # Without temperatures we can only keep the previous value
        percent = 0.0
        pid_dbg = {"mode": "pid", "error": "no_temps"}
        return percent, pid_dbg

    delta_T = inp_target_temp_C - inp_current_temp_C
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
            # Mischung aus externer und TRV-interner Temperatur für die Ableitung
            try:
                mix = max(0.0, min(1.0, float(params.trend_mix_trv)))
            except (TypeError, ValueError):
                mix = 0.0
            meas_now = None
            if inp_current_temp_C is not None:
                if inp_trv_temp_C is not None:
                    # mix = Anteil EXTERN, (1-mix) = Anteil INTERN
                    meas_now = (mix * inp_current_temp_C) + (
                        (1.0 - mix) * inp_trv_temp_C
                    )
                else:
                    meas_now = inp_current_temp_C
            if meas_now is not None:
                # EMA-Glättung nur für den D-Kanal
                try:
                    a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
                except (TypeError, ValueError):
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
        u_sat = max(0.0, min(100.0, u_prop))
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
    # Valve slew-rate limiter
    percent_unlimited = max(0.0, min(100.0, u))
    change = percent_unlimited - st.last_percent
    max_change = params.slew_rate
    if abs(change) > max_change:
        change = max_change if change > 0 else -max_change
    percent = st.last_percent + change
    # Clamp auf 0..100
    percent = max(0.0, min(100.0, percent))
    # Update last_percent
    st.last_percent = percent

    # PID-States aktualisieren (für D-Anteil gemischten Messwert speichern)
    if params.d_on_measurement:
        base = inp_current_temp_C
        try:
            mix = max(0.0, min(1.0, float(params.trend_mix_trv)))
        except (TypeError, ValueError):
            mix = 0.0
        if base is not None and inp_trv_temp_C is not None:
            # mix = Anteil EXTERN auf base; (1-mix) = Anteil INTERN
            base = (mix * base) + ((1.0 - mix) * inp_trv_temp_C)
        try:
            a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
        except (TypeError, ValueError):
            a = 0.5
        if base is not None:
            prev = st.pid_last_meas
            st.pid_last_meas = base if prev is None else ((1.0 - a) * prev + a * base)
    else:
        st.pid_last_meas = inp_current_temp_C
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
        # Mischgewichte für Transparenz berechnen
        try:
            _mix_ext = max(0.0, min(1.0, float(params.trend_mix_trv)))
        except (TypeError, ValueError):
            _mix_ext = None
        _mix_int = (1.0 - _mix_ext) if isinstance(_mix_ext, float) else None
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
            # Messwerte & Mischanteile
            "meas_external_C": _r(inp_current_temp_C, 2),
            "meas_trv_C": _r(inp_trv_temp_C, 2),
            "mix_w_internal": (_r(_mix_int, 2) if _mix_int is not None else None),
            "mix_w_external": (_r(_mix_ext, 2) if _mix_ext is not None else None),
            "meas_blend_C": _r(meas_now, 2),
            "meas_smooth_C": _r(smoothed, 2),
            "d_meas_per_s": _r(d_meas, 4),
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

    return percent, pid_dbg


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

        # 1) Overshoot → kp leicht runter, kd leicht rauf
        if overshoot:
            kp = max(params.kp_min, kp * params.kp_step_mul)
            kd = min(params.kd_max, kd * params.kd_step_mul)
            tuned = True

        # 2) Trägheit: ΔT deutlich > band_near, aber Slope sehr klein -> Ki leicht rauf (only near steady-state)
        if (
            delta_T > params.steady_state_band_K
            and abs(slope) < params.sluggish_slope_threshold_K_min
            and abs(delta_T) < 1.0  # Restrict to near steady-state
        ):
            ki = min(params.ki_max, max(params.ki_min, ki * params.ki_step_mul_up))
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
    except (ValueError, TypeError):
        # Best-effort: numerische Probleme ignorieren
        return


def reset_pid_state(key: str) -> None:
    """Reset learned/smoothing values for a given room key."""
    if key in _PID_STATES:
        del _PID_STATES[key]


def get_pid_state(key: str) -> PIDState | None:
    """Return the PIDState for key or None if missing.

    This is a small helper used externally to read persisted/learned gains.
    """
    return _PID_STATES.get(key)


def seed_pid_gains(key: str, kp: float, ki: float, kd: float) -> bool:
    """Seed PID gains for a given key, creating state if missing.

    Args:
        key: PID state key (format: {unique_id}:{entity_id}:{bucket})
        kp: Proportional gain
        ki: Integral gain
        kd: Derivative gain

    Returns
    -------
        True if gains were successfully seeded
    """
    if key not in _PID_STATES:
        _PID_STATES[key] = PIDState()
    state = _PID_STATES[key]
    state.pid_kp = kp
    state.pid_ki = ki
    state.pid_kd = kd
    return True


# --- Key Builder Helper -----------------------------------------------


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
            f"t{round(float(tcur) * 2.0) / 2.0:.1f}"
            if isinstance(tcur, (int, float))
            else "tunknown"
        )
    except Exception:
        bucket_tag = "tunknown"

    # Use public unique_id property if available, fallback to _unique_id or "bt"
    uid = getattr(self, "unique_id", None) or getattr(self, "_unique_id", "bt")
    return f"{uid}:{entity_id}:{bucket_tag}"


# --- Persistence helpers --------------------------------------------


def export_pid_states(prefix: str | None = None) -> dict[str, dict[str, Any]]:
    """Export internal PID state to a JSON-serializable dict.

    prefix: if provided, only include keys starting with this prefix.
    """
    out: dict[str, dict[str, Any]] = {}
    for k, st in _PID_STATES.items():
        if prefix is not None and not k.startswith(prefix):
            continue
        out[k] = {
            "pid_integral": st.pid_integral,
            "pid_last_meas": st.pid_last_meas,
            "pid_last_time": st.pid_last_time,
            "pid_kp": st.pid_kp,
            "pid_ki": st.pid_ki,
            "pid_kd": st.pid_kd,
            "auto_tune": st.auto_tune,
            "last_tune_ts": st.last_tune_ts,
            "last_delta_sign": st.last_delta_sign,
            "last_error_sign": st.last_error_sign,
            "previous_abs_error": st.previous_abs_error,
            "last_abs_error": st.last_abs_error,
            "ema_slope": st.ema_slope,
            "last_percent": st.last_percent,
        }
    return out


def import_pid_states(
    data: dict[str, dict[str, Any]], prefix_filter: str | None = None
) -> int:
    """Import previously saved PID states into the module-local cache.

    Returns the number of imported entries. If prefix_filter is provided,
    only keys starting with that prefix will be imported.
    """
    if not isinstance(data, dict):
        return 0
    count = 0
    for k, v in data.items():
        if prefix_filter is not None and not str(k).startswith(prefix_filter):
            continue
        try:
            st = PIDState(
                pid_integral=v.get("pid_integral", 0.0),
                pid_last_meas=v.get("pid_last_meas"),
                pid_last_time=v.get("pid_last_time", 0.0),
                pid_kp=v.get("pid_kp"),
                pid_ki=v.get("pid_ki"),
                pid_kd=v.get("pid_kd"),
                auto_tune=v.get("auto_tune"),
                last_tune_ts=v.get("last_tune_ts", 0.0),
                last_delta_sign=v.get("last_delta_sign"),
                last_error_sign=v.get("last_error_sign"),
                previous_abs_error=v.get("previous_abs_error"),
                last_abs_error=v.get("last_abs_error"),
                ema_slope=v.get("ema_slope"),
                last_percent=v.get("last_percent", 0.0),
            )
            _PID_STATES[str(k)] = st
            count += 1
        except (KeyError, TypeError, ValueError):
            # Ignore malformed entries
            continue
    return count
