"""
Hydraulic balance helper (decentralized, no valve feedback).

Goals:
- Derive a desired valve opening percentage from present/target/temperature trend (ΔT, dT/dt).
- For TRVs without direct valve control, compute an equivalent setpoint reduction
    (flow cap in Kelvin): setpoint_eff = target - flow_cap (only near the setpoint).
- For Sonoff TRVZB additionally propose min_open% and max_open% values.

Notes:
- No side effects: this module only computes recommendations; writing to the device
    stays in adapters/controlling.
- Lightweight per-room state by a `key` (e.g., entity_id): EMA, hysteresis, rate limit.

Modes:
- heuristic: simple rule-based mapping of ΔT & slope
- pid: classic PID with conservative auto-tuning
- mpc: lightweight predictive control (finite horizon minimization of future error + penalties)

Integration:
- controlling.convert_outbound_states() can call `compute_balance(...)` and depending
    on device either use `set_valve(percent)` or `set_temperature(setpoint_eff)`.
- For Sonoff: write min/max open via a dedicated adapter implementation if available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from time import monotonic
import logging


# --- Key Builder Helper -----------------------------------------------


def build_balance_key(self, entity_id: str) -> str:
    """Build consistent balance state key across all modules.

    Format: {unique_id}:{entity_id}:t{target_temp:.1f}
    where target_temp is rounded to 0.5°C buckets.

    Args:
        self: BetterThermostat instance with unique_id and bt_target_temp
        entity_id: TRV entity ID

    Returns:
        Balance key string
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


# --- Default Parameter -----------------------------------------------


@dataclass
class BalanceParams:
    # Algorithmus-Auswahl: 'heuristic' (Standard), 'pid', 'mpc'
    mode: str = "heuristic"
    # Near setpoint band where we gently throttle/refine (Kelvin)
    band_near_K: float = 0.1
    # Far band beyond which fully open/close is likely (Kelvin)
    band_far_K: float = 0.3
    # Max setpoint reduction for generic devices (Kelvin)
    cap_max_K: float = 0.8
    # Slope thresholds (K/min)
    slope_up_K_per_min: float = 0.02  # fast rising → throttle more
    slope_down_K_per_min: float = -0.01  # falling while demand → open more
    # Slope gain (percentage points per K/min, negative means positive slope reduces %)
    slope_gain_per_K_per_min: float = -1000.0  # 0.02 K/min → ~ -20%
    # Smoothing and hysteresis
    percent_smoothing_alpha: float = 0.35  # EMA (höher = schneller)
    percent_hysteresis_pts: float = 0.5  # minimum change in %-points
    min_update_interval_s: float = 60.0  # minimum time between updates
    # Sonoff minimum opening (comfort/flow noise)
    sonoff_min_open_default_pct: int = 5
    # PID-Parameter (optional)
    kp: float = 100.0
    ki: float = 0.03
    kd: float = 2000.0
    # Integrator-Klammer (Anti-Windup) in %-Punkten umgerechnet
    i_min: float = -60.0
    i_max: float = 60.0
    # Ableitung auf Messwert (True) oder Fehler
    d_on_measurement: bool = True
    # Anteil der EXTERNEN Temperatur für den D-Anteil (0..1); 0=nur intern (TRV), 1=nur extern
    trend_mix_trv: float = 0.7
    # Separate Glättung (EMA) des Messwerts für den D-Anteil (0..1)
    d_smoothing_alpha: float = 0.6
    # Auto-Tuning (konservativ, optional)
    auto_tune: bool = True
    tune_min_interval_s: float = 1800.0  # mind. 30min zwischen Anpassungen
    # Gain-Grenzen
    kp_min: float = 5.0
    kp_max: float = 500.0
    ki_min: float = 0.0
    ki_max: float = 1.0
    kd_min: float = 0.0
    kd_max: float = 10000.0
    # Lernraten (Multiplikatoren)
    kp_step_mul: float = 0.9  # bei Overshoot: kp *= 0.9
    kd_step_mul: float = 1.1  # bei Overshoot: kd *= 1.1
    ki_step_mul_down: float = 0.9
    ki_step_mul_up: float = 1.1
    # Kriterien
    overshoot_threshold_K: float = 0.2
    sluggish_slope_threshold_K_min: float = 0.005
    steady_state_band_K: float = 0.1
    steady_state_duration_s: float = 900.0
    # MPC-Parameter (Experimental)
    mpc_horizon_steps: int = 12  # Anzahl Vorwärtsschritte
    # Schrittweite in Sekunden (simulativ, nicht real-time tick)
    mpc_step_s: float = 60.0
    # Effekt pro Schritt (Temperatur-Fehlerabbau pro 100% Ventil)
    mpc_thermal_gain: float = 0.1
    mpc_loss_coeff: float = 0.01  # Fehler-Rückfall (Leak) pro Schritt
    mpc_control_penalty: float = 0.0003  # Gewicht auf hohe Ventilöffnung
    mpc_change_penalty: float = 0.05  # Gewicht auf Änderung gegenüber letztem Wert
    mpc_adapt: bool = True  # Einfache Online-Anpassung der Gain/Leak Parameter
    mpc_gain_min: float = 0.005
    mpc_gain_max: float = 0.5
    mpc_loss_min: float = 0.0
    mpc_loss_max: float = 0.05
    mpc_deadzone_min: float = 0.0
    mpc_deadzone_max: float = 20.0
    mpc_adapt_alpha: float = 0.2  # Lernrate für adaptives Modell


# --- I/O and state types ---------------------------------------------


@dataclass
class BalanceInput:
    key: str  # z.B. Entity-ID
    target_temp_C: Optional[float]
    current_temp_C: Optional[float]
    trv_temp_C: Optional[float] = None
    tolerance_K: float = 0.0
    temp_slope_K_per_min: Optional[float] = None
    window_open: bool = False
    heating_allowed: bool = True  # z.B. HVAC != OFF und kein Fenster offen


@dataclass
class BalanceOutput:
    # Primary actuator (0..100)
    valve_percent: int
    # For generic devices (setpoint only):
    flow_cap_K: float  # >= 0; effective setpoint = target - flow_cap
    setpoint_eff_C: Optional[float]
    # For Sonoff TRVZB (optional):
    sonoff_min_open_pct: int
    sonoff_max_open_pct: int
    # Debug
    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BalanceState:
    last_percent: Optional[float] = None
    last_update_ts: float = 0.0
    ema_slope: Optional[float] = None
    # Letzter Zielwert zur Erkennung von Setpoint-Änderungen
    last_target_C: Optional[float] = None
    # PID-State
    pid_integral: float = 0.0
    pid_last_meas: Optional[float] = None
    pid_last_time: float = 0.0
    # Lernende Gains (persistierbar)
    pid_kp: Optional[float] = None
    pid_ki: Optional[float] = None
    pid_kd: Optional[float] = None
    last_tune_ts: float = 0.0
    # Heuristik-Zustände
    last_delta_sign: Optional[int] = None
    ss_band_entry_ts: float = 0.0
    heat_sat_entry_ts: float = 0.0
    # Letztes Vorzeichen des Fehlers zur Erkennung von Flip-Events
    last_error_sign: Optional[int] = None
    # MPC Modell-Schätzung
    mpc_gain_est: Optional[float] = None
    mpc_loss_est: Optional[float] = None
    mpc_deadzone_est: Optional[float] = None
    mpc_last_temp: Optional[float] = None
    mpc_last_trv_temp: Optional[float] = None
    mpc_last_time: float = 0.0
    # MPC Deadzone-Kalibrierung (aktiv)
    mpc_deadzone_test_active: bool = False
    mpc_deadzone_test_cooling: bool = False  # Abkühlphase vor Test
    mpc_deadzone_test_cooling_start: float = 0.0
    mpc_deadzone_test_u: float = 0.0
    mpc_deadzone_test_start_ts: float = 0.0
    mpc_deadzone_test_trv_start: Optional[float] = None
    mpc_deadzone_test_failed_ts: float = 0.0  # Letzter fehlgeschlagener Test
    mpc_deadzone_last_log: float = 0.0


_LOGGER = logging.getLogger(__name__)

# Module-local storage
_BALANCE_STATES: Dict[str, BalanceState] = {}

# Global deadzone cache: key = "{uid}:{trv_id}" (without temperature bucket)
# Deadzone is a mechanical property of the TRV, independent of target temperature
_DEADZONE_CACHE: Dict[str, Optional[float]] = {}


# --- Core computation -------------------------------------------------


def compute_balance(
    inp: BalanceInput, params: BalanceParams = BalanceParams()
) -> BalanceOutput:
    """Compute a decentralized valve percentage from ΔT and trend.

    Logic (heat-only, no valve feedback):
    - Large positive ΔT (>= band_far) → 100% (fully open)
    - Large negative ΔT (<= -band_far) → 0% (closed/hold)
    - Near setpoint (|ΔT| < band_far) map linearly to 0..100% and adjust by slope:
            * positive slope → reduce opening (prevent overshoot)
            * negative slope with ΔT>0 → ensure sufficient opening
    - Output is smoothed (EMA), hysteretic and rate-limited.

    Additionally, derive a setpoint reduction (flow_cap_K) to emulate throttling
    on devices without a direct valve command.

    Also returns Sonoff-specific min/max opening percentages (recommended).
    """
    now = monotonic()
    st = _BALANCE_STATES.setdefault(inp.key, BalanceState())

    # Extract TRV identifier from key (format: uid:trv_id:bucket)
    # Deadzone is TRV-specific, not temperature-specific
    try:
        key_parts = inp.key.rsplit(":", 1)  # Split off last part (bucket)
        trv_key = key_parts[0] if len(key_parts) > 1 else inp.key
    except Exception:
        trv_key = inp.key

    # Use global deadzone cache if state doesn't have deadzone yet
    if st.mpc_deadzone_est is None and trv_key in _DEADZONE_CACHE:
        st.mpc_deadzone_est = _DEADZONE_CACHE[trv_key]

    # Debug: Log calibration state at start of compute_balance
    _LOGGER.debug(
        "compute_balance(%s): ENTRY - cooling=%s active=%s deadzone=%s",
        inp.key,
        st.mpc_deadzone_test_cooling,
        st.mpc_deadzone_test_active,
        st.mpc_deadzone_est,
    )

    # Ensure pid_dbg exists for static analyzers; will be populated in PID branch
    pid_dbg: Dict[str, Any] = {}

    # Helper to round values for debug/logging only
    def _r(x: Any, n: int = 2):
        try:
            return round(float(x), n) if x is not None else None
        except (TypeError, ValueError):
            return x

    # Fail-safe defaults
    percent_base = None
    delta_T: Optional[float] = None
    if not inp.heating_allowed or inp.window_open:
        percent = 0.0
    else:
        # Delta T
        if inp.target_temp_C is None or inp.current_temp_C is None:
            # Without temperatures we can only keep the previous value
            percent = st.last_percent if st.last_percent is not None else 0.0
        else:
            delta_T = inp.target_temp_C - inp.current_temp_C

            mode_lower = params.mode.lower()
            if mode_lower == "mpc":
                # --- MPC Regelung (prädiktiv) ---------------------------------
                # Debug: Check calibration state
                if st.mpc_deadzone_test_cooling or st.mpc_deadzone_test_active:
                    _LOGGER.debug(
                        "MPC calibration active: cooling=%s active=%s deadzone_est=%s",
                        st.mpc_deadzone_test_cooling,
                        st.mpc_deadzone_test_active,
                        st.mpc_deadzone_est,
                    )

                # Fehler (Soll - Ist)
                e0 = delta_T
                dt_last = now - st.mpc_last_time if st.mpc_last_time > 0 else 0.0

                # Aktive Deadzone-Kalibrierung (einmalig beim Start oder bei Reset)
                if st.mpc_deadzone_test_cooling:
                    # Abkühlphase: Warte 5 Min bei 0% bis TRV abgekühlt ist
                    cooling_duration = now - st.mpc_deadzone_test_cooling_start
                    if cooling_duration >= 300.0:  # 5 Min Abkühlung
                        # Abkühlung fertig, starte eigentlichen Test
                        st.mpc_deadzone_test_cooling = False
                        st.mpc_deadzone_test_active = True
                        st.mpc_deadzone_test_u = 2.0
                        st.mpc_deadzone_test_start_ts = now
                        st.mpc_deadzone_test_trv_start = inp.trv_temp_C
                        st.mpc_deadzone_last_log = 0.0
                        _LOGGER.debug(
                            "MPC deadzone calibration: cooling finished for %s, starting test at %.1f%% (trv_temp=%s)",
                            trv_key,
                            st.mpc_deadzone_test_u,
                            _r(inp.trv_temp_C, 2),
                        )
                        percent = st.mpc_deadzone_test_u
                    else:
                        # Noch in Abkühlphase, TRV auf 0%
                        percent = 0.0
                        if (
                            st.mpc_deadzone_last_log == 0.0
                            or now - st.mpc_deadzone_last_log >= 60.0
                        ):
                            remaining = max(0.0, 300.0 - cooling_duration)
                            _LOGGER.debug(
                                "MPC deadzone calibration: cooling %s (elapsed=%.0fs remaining=%.0fs)",
                                trv_key,
                                cooling_duration,
                                remaining,
                            )
                            st.mpc_deadzone_last_log = now
                elif st.mpc_deadzone_test_active:
                    # Test läuft bereits
                    test_duration = now - st.mpc_deadzone_test_start_ts
                    if (
                        test_duration >= 180.0 and inp.trv_temp_C is not None
                    ):  # 3 Min Wartezeit
                        # Prüfe TRV-Reaktion
                        if st.mpc_deadzone_test_trv_start is not None:
                            trv_rise = inp.trv_temp_C - st.mpc_deadzone_test_trv_start
                            if (
                                trv_rise > 0.1
                            ):  # Deutliche Erwärmung → Deadzone gefunden!
                                st.mpc_deadzone_est = max(
                                    0.0, st.mpc_deadzone_test_u - 1.0
                                )
                                st.mpc_deadzone_test_active = False
                                st.mpc_deadzone_last_log = 0.0
                                # Save to global cache so other temperature buckets can use it
                                try:
                                    _DEADZONE_CACHE[trv_key] = st.mpc_deadzone_est
                                    _LOGGER.debug(
                                        "MPC deadzone calibration: response detected at %.1f%% for %s (rise=%.3fK, saved deadzone=%.1f%%)",
                                        st.mpc_deadzone_test_u,
                                        trv_key,
                                        trv_rise,
                                        st.mpc_deadzone_est,
                                    )
                                except Exception:
                                    pass
                                # Weiter mit normaler Regelung
                            elif st.mpc_deadzone_test_u >= 20.0:
                                # Maximaler Test erreicht ohne TRV-Reaktion
                                # → Wahrscheinlich Boiler aus oder kein Warmwasser
                                # Markiere als fehlgeschlagen und versuche später erneut
                                st.mpc_deadzone_test_active = False
                                st.mpc_deadzone_test_failed_ts = now
                                # Setze provisorischen Wert um weiterzuarbeiten
                                st.mpc_deadzone_est = 8.0
                                st.mpc_deadzone_last_log = 0.0
                                _LOGGER.warning(
                                    "MPC deadzone calibration: no response up to %.1f%% for %s, using fallback deadzone %.1f%%",
                                    st.mpc_deadzone_test_u,
                                    trv_key,
                                    st.mpc_deadzone_est,
                                )
                                # Test wird automatisch wiederholt wenn:
                                # - mindestens 30 Min seit letztem Fehlschlag vergangen
                                # - System deutlich heizt (TRV steigt ohne Test)
                            else:
                                # Nächster Test-Schritt
                                st.mpc_deadzone_test_u += 2.0
                                st.mpc_deadzone_test_start_ts = now
                                st.mpc_deadzone_test_trv_start = inp.trv_temp_C
                                st.mpc_deadzone_last_log = 0.0
                                _LOGGER.debug(
                                    "MPC deadzone calibration: escalating test for %s to %.1f%% (elapsed %.0fs)",
                                    trv_key,
                                    st.mpc_deadzone_test_u,
                                    test_duration,
                                )
                                percent = st.mpc_deadzone_test_u  # Force test value
                        else:
                            # Initialisiere Startwert
                            st.mpc_deadzone_test_trv_start = inp.trv_temp_C
                            _LOGGER.debug(
                                "MPC deadzone calibration: captured initial TRV temperature %s for %s",
                                _r(inp.trv_temp_C, 2),
                                trv_key,
                            )
                    else:
                        # Warte weiter, setze Test-Wert
                        percent = st.mpc_deadzone_test_u
                        if (
                            st.mpc_deadzone_last_log == 0.0
                            or now - st.mpc_deadzone_last_log >= 60.0
                        ):
                            _LOGGER.debug(
                                "MPC deadzone calibration: holding %.1f%% for %s (elapsed %.0fs of 180s)",
                                st.mpc_deadzone_test_u,
                                trv_key,
                                test_duration,
                            )
                            st.mpc_deadzone_last_log = now
                elif (
                    st.mpc_deadzone_est is None
                    and not st.mpc_deadzone_test_cooling
                    and not st.mpc_deadzone_test_active
                    and inp.trv_temp_C is not None
                ):
                    # Starte Deadzone-Test beim ersten Mal (unabhängig von e0)
                    # oder wenn System kalt genug ist (e0 >= 0.15K = 0.15K unter Ziel)
                    should_start = False

                    # Prüfe ob vorheriger Test fehlgeschlagen ist (Boiler war aus)
                    time_since_failed = (
                        now - st.mpc_deadzone_test_failed_ts
                        if st.mpc_deadzone_test_failed_ts > 0
                        else 9999.0
                    )

                    # Wenn Test kürzlich fehlschlug, warte mind. 30 Min bevor erneuter Versuch
                    # UND prüfe ob TRV aktuell steigt (zeigt: Boiler ist jetzt aktiv)
                    if time_since_failed < 1800.0:  # 30 Min
                        # Prüfe ob TRV gerade steigt → Boiler läuft jetzt
                        if (
                            st.mpc_last_trv_temp is not None
                            and inp.trv_temp_C is not None
                        ):
                            trv_rising = inp.trv_temp_C - st.mpc_last_trv_temp
                            if (
                                trv_rising > 0.5 and dt_last > 120
                            ):  # Deutlicher Anstieg über 2+ Min
                                # Boiler scheint jetzt zu laufen, versuche Test erneut
                                should_start = True
                                st.mpc_deadzone_test_failed_ts = (
                                    0.0  # Reset failed marker
                                )
                    else:
                        # Genug Zeit vergangen oder noch nie getestet
                        if e0 is None:
                            # Kein Fehler bekannt, starte trotzdem beim ersten Mal
                            should_start = True
                        elif e0 >= 0.15:
                            # System ist kalt genug (min 0.15K unter Ziel)
                            should_start = True

                    if should_start:
                        # Starte mit Abkühlphase (5 Min bei 0%)
                        st.mpc_deadzone_test_cooling = True
                        st.mpc_deadzone_test_cooling_start = now
                        st.mpc_deadzone_test_active = False
                        st.mpc_deadzone_last_log = 0.0
                        percent = 0.0  # TRV abkühlen lassen
                        if time_since_failed < 1800.0:
                            reason = "retry after failure"
                        elif e0 is not None and e0 >= 0.15:
                            reason = f"delta_T={_r(e0, 2)}K below target"
                        else:
                            reason = "initial calibration"
                        _LOGGER.debug(
                            "MPC deadzone calibration: starting cooling phase for %s (%s)",
                            trv_key,
                            reason,
                        )

                # Normale MPC-Regelung (nur wenn kein Test aktiv und keine Abkühlphase läuft)
                if not st.mpc_deadzone_test_active and not st.mpc_deadzone_test_cooling:
                    # Adaptive Modellschätzung (optional)
                    if (
                        params.mpc_adapt
                        and st.mpc_last_temp is not None
                        and inp.current_temp_C is not None
                        and inp.target_temp_C is not None
                        and dt_last > 0
                    ):
                        try:
                            e_prev = inp.target_temp_C - st.mpc_last_temp
                            e_now = inp.target_temp_C - inp.current_temp_C
                            u_last = max(
                                0.0,
                                min(
                                    100.0,
                                    (
                                        st.last_percent
                                        if st.last_percent is not None
                                        else 0.0
                                    ),
                                ),
                            )
                            if st.mpc_gain_est is None:
                                st.mpc_gain_est = params.mpc_thermal_gain
                            if st.mpc_loss_est is None:
                                st.mpc_loss_est = params.mpc_loss_coeff
                            if st.mpc_deadzone_est is None:
                                st.mpc_deadzone_est = 0.0
                            if e_prev != 0:
                                decay_observed = (
                                    e_prev - e_now
                                )  # positiver Wert = Fehler nimmt ab

                                # Deadzone-Learning via TRV-Temperatur (konservativ mit Zeitfilter)
                                trv_effect = None
                                if (
                                    inp.trv_temp_C is not None
                                    and st.mpc_last_trv_temp is not None
                                ):
                                    # TRV-Temperaturänderung (positiv = Erwärmung)
                                    trv_effect = inp.trv_temp_C - st.mpc_last_trv_temp

                                # Deadzone-Erkennung: nutze TRV wenn verfügbar, sonst Raumtemperatur
                                # WICHTIG: Nur bei ausreichendem Zeitabstand (mind. 120s), sonst zu frühe Erkennung
                                if trv_effect is not None and dt_last >= 120.0:
                                    # TRV-basiert: Reaktion braucht 2-5 Minuten
                                    if u_last > 0 and u_last <= 15.0:
                                        # Keine Wirkung erkannt: TRV steigt nicht trotz Ventilöffnung
                                        if (
                                            abs(trv_effect) < 0.01
                                        ):  # sehr strikt: <0.01K
                                            # Deadzone LANGSAM erhöhen (nur ~0.3% pro Zyklus bei α=0.015)
                                            dz_candidate = u_last + 1.0
                                            st.mpc_deadzone_est = (
                                                1 - params.mpc_adapt_alpha * 0.015
                                            ) * st.mpc_deadzone_est + (
                                                params.mpc_adapt_alpha * 0.015
                                            ) * dz_candidate
                                    # Wirkung erkannt unterhalb bisheriger Deadzone: Deadzone zu hoch geschätzt
                                    if (
                                        trv_effect > 0.08  # deutliche Erwärmung
                                        and u_last < st.mpc_deadzone_est
                                    ):
                                        # Deadzone SCHNELL reduzieren
                                        dz_candidate = max(0.0, u_last - 0.5)
                                        st.mpc_deadzone_est = (
                                            1 - params.mpc_adapt_alpha * 0.5
                                        ) * st.mpc_deadzone_est + (
                                            params.mpc_adapt_alpha * 0.5
                                        ) * dz_candidate
                                elif trv_effect is None and dt_last >= 300.0:
                                    # Fallback: Raumtemperatur-basiert (sehr langsam, nur bei 5+ Min Abstand)
                                    if (
                                        u_last > 0
                                        and u_last <= 15.0
                                        and abs(decay_observed) < 0.03
                                    ):
                                        # Keine Raumtemperatur-Wirkung bei niedrigem u → Deadzone erhöhen
                                        dz_candidate = u_last + 1.0
                                        st.mpc_deadzone_est = (
                                            1 - params.mpc_adapt_alpha * 0.01
                                        ) * st.mpc_deadzone_est + (
                                            params.mpc_adapt_alpha * 0.01
                                        ) * dz_candidate

                                # Gain-Learning: nur oberhalb Deadzone
                                if u_last > st.mpc_deadzone_est and decay_observed > 0:
                                    # Wirkung oberhalb Deadzone → Gain lernen
                                    u_effective = u_last - st.mpc_deadzone_est
                                    if u_effective > 0:
                                        gain_est_candidate = (
                                            decay_observed / abs(e_prev)
                                        ) * (100.0 / u_effective)
                                        # Glättung
                                        st.mpc_gain_est = (
                                            (1 - params.mpc_adapt_alpha)
                                            * st.mpc_gain_est
                                            + params.mpc_adapt_alpha
                                            * gain_est_candidate
                                        )
                                # Leak / Verlust (Fehler-Rückkehr)
                                leak_raw = e_now - e_prev  # >0: Fehler nimmt zu
                                if e_prev != 0:
                                    loss_est_candidate = max(
                                        0.0, leak_raw / abs(e_prev)
                                    )
                                    st.mpc_loss_est = (
                                        (1 - params.mpc_adapt_alpha) * st.mpc_loss_est
                                        + params.mpc_adapt_alpha * loss_est_candidate
                                    )
                            # Clamp
                            st.mpc_gain_est = max(
                                params.mpc_gain_min,
                                min(params.mpc_gain_max, st.mpc_gain_est),
                            )
                            st.mpc_loss_est = max(
                                params.mpc_loss_min,
                                min(params.mpc_loss_max, st.mpc_loss_est),
                            )
                            st.mpc_deadzone_est = max(
                                params.mpc_deadzone_min,
                                min(params.mpc_deadzone_max, st.mpc_deadzone_est),
                            )
                        except Exception:
                            pass

                    gain = (
                        st.mpc_gain_est
                        if st.mpc_gain_est is not None
                        else params.mpc_thermal_gain
                    )
                    loss = (
                        st.mpc_loss_est
                        if st.mpc_loss_est is not None
                        else params.mpc_loss_coeff
                    )
                    deadzone = (
                        st.mpc_deadzone_est if st.mpc_deadzone_est is not None else 0.0
                    )
                    horizon = max(1, int(params.mpc_horizon_steps))
                    ctrl_pen = max(0.0, float(params.mpc_control_penalty))
                    chg_pen = max(0.0, float(params.mpc_change_penalty))
                    last_p = st.last_percent if st.last_percent is not None else None
                    best_u = 0.0
                    best_cost = None
                    eval_count = 0
                    # Kandidaten (2%-Raster für feinere Auflösung)
                    for u_candidate in range(0, 101, 2):
                        e = e0 if e0 is not None else 0.0
                        cost = 0.0
                        # Vorwärtssimulation
                        for _ in range(horizon):
                            # Fehler-Dynamik mit Deadzone: e_{k+1} = e_k*(1+loss) - gain*max(0, u-deadzone)/100
                            u_effective = max(0.0, u_candidate - deadzone)
                            e = e * (1.0 + loss) - gain * (u_effective / 100.0)
                            cost += e * e
                            eval_count += 1
                        # Strafen
                        cost += ctrl_pen * (u_candidate * u_candidate)
                        if last_p is not None:
                            cost += chg_pen * abs(u_candidate - last_p)
                        if best_cost is None or cost < best_cost:
                            best_cost = cost
                            best_u = float(u_candidate)
                    percent = best_u
                    # Debug-Infos für MPC
                    try:
                        pid_dbg = {  # reuse key 'pid' later but mark mode
                            "mode": "mpc",
                            "e0_K": _r(e0, 3),
                            "gain": _r(gain, 4),
                            "loss": _r(loss, 4),
                            "deadzone": _r(deadzone, 2),
                            "horizon": horizon,
                            "candidate_step_pct": 2,
                            "eval_count": eval_count,
                            "last_percent": _r(last_p, 2),
                            "best_u": _r(best_u, 2),
                            "cost": _r(best_cost, 3),
                        }
                    except Exception:
                        pid_dbg = {"mode": "mpc"}
                # Update Modell-Zustände
                if not st.mpc_deadzone_test_active:
                    st.mpc_last_temp = inp.current_temp_C
                    st.mpc_last_trv_temp = inp.trv_temp_C
                    st.mpc_last_time = now
            elif mode_lower == "pid":
                # PID-Regelung
                # Zeitdifferenz
                dt = now - st.pid_last_time if st.pid_last_time > 0 else 0.0
                # Fehler
                e = delta_T
                # Initialisiere lernende Gains (einmalig) mit übergebenen Params
                if st.pid_kp is None:
                    st.pid_kp = params.kp
                if st.pid_ki is None:
                    st.pid_ki = params.ki
                if st.pid_kd is None:
                    st.pid_kd = params.kd
                # Integrator (nur wenn dt>0)
                if dt > 0:
                    st.pid_integral += float(st.pid_ki) * e * dt
                    # Anti-Windup (Integrator klammern)
                    st.pid_integral = max(
                        params.i_min, min(params.i_max, st.pid_integral)
                    )
                # Ableitung
                d_term = 0.0
                # Für Debugging/Graphen
                p_term: Optional[float] = None
                i_term: Optional[float] = None
                u: Optional[float] = None
                meas_now: Optional[float] = None
                smoothed: Optional[float] = None
                d_meas: Optional[float] = None
                if params.mode.lower() == "pid":
                    if params.d_on_measurement:
                        if dt > 0:
                            # Mischung aus externer und TRV-interner Temperatur für die Ableitung
                            try:
                                mix = max(0.0, min(1.0, float(params.trend_mix_trv)))
                            except (TypeError, ValueError):
                                mix = 0.0
                            meas_now = None
                            if inp.current_temp_C is not None:
                                if inp.trv_temp_C is not None:
                                    # mix = Anteil EXTERN, (1-mix) = Anteil INTERN
                                    meas_now = (mix * inp.current_temp_C) + (
                                        (1.0 - mix) * inp.trv_temp_C
                                    )
                                else:
                                    meas_now = inp.current_temp_C
                            if meas_now is not None:
                                # EMA-Glättung nur für den D-Kanal
                                try:
                                    a = max(
                                        0.0, min(1.0, float(params.d_smoothing_alpha))
                                    )
                                except (TypeError, ValueError):
                                    a = 0.5
                                prev = st.pid_last_meas
                                smoothed = (
                                    meas_now
                                    if prev is None
                                    else ((1.0 - a) * prev + a * meas_now)
                                )
                                if prev is not None:
                                    d_meas = (smoothed - prev) / dt
                                    d_term = -float(st.pid_kd) * d_meas
                                # Update des gespeicherten (geglätteten) Messwerts erfolgt nach u-Berechnung unten
                    else:
                        # Derivative on error (benötigt letzten Fehler – approximiert über letzten Messwert)
                        if dt > 0 and st.pid_last_meas is not None:
                            last_e = inp.target_temp_C - st.pid_last_meas
                            d_err = (e - last_e) / dt
                            d_term = float(st.pid_kd) * d_err
                # Aktualisiere die Slope-EMA auch im PID-Modus (für Logging/Diagnose)
                try:
                    s_in = inp.temp_slope_K_per_min
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
                        and cur_sign != 0
                        and cur_sign != st.last_error_sign
                        and abs(delta_T or 0.0) <= params.band_near_K
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
                # Clamp auf 0..100
                percent = max(0.0, min(100.0, u))
                # PID-States aktualisieren (für D-Anteil gemischten Messwert speichern)
                # PID-States aktualisieren: für D-Anteil den geglätteten Messwert persistieren
                if params.d_on_measurement:
                    base = inp.current_temp_C
                    try:
                        mix = max(0.0, min(1.0, float(params.trend_mix_trv)))
                    except (TypeError, ValueError):
                        mix = 0.0
                    if base is not None and inp.trv_temp_C is not None:
                        # mix = Anteil EXTERN auf base; (1-mix) = Anteil INTERN
                        base = (mix * base) + ((1.0 - mix) * inp.trv_temp_C)
                    try:
                        a = max(0.0, min(1.0, float(params.d_smoothing_alpha)))
                    except (TypeError, ValueError):
                        a = 0.5
                    if base is not None:
                        prev = st.pid_last_meas
                        st.pid_last_meas = (
                            base if prev is None else ((1.0 - a) * prev + a * base)
                        )
                else:
                    st.pid_last_meas = inp.current_temp_C
                st.pid_last_time = now
                # Fehler-Vorzeichen für nächsten Zyklus merken
                try:
                    st.last_error_sign = 1 if e > 0 else (-1 if e < 0 else 0)
                except Exception:
                    pass
                # Optionales Auto-Tuning (konservativ)
                if params.auto_tune:
                    _auto_tune_pid(
                        params,
                        st,
                        percent,
                        delta_T,
                        inp.temp_slope_K_per_min or 0.0,
                        now,
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
                        "slope_in": _r(inp.temp_slope_K_per_min, 3),
                        "slope_ema": _r(st.ema_slope, 3),
                        # Messwerte & Mischanteile
                        "meas_external_C": _r(inp.current_temp_C, 2),
                        "meas_trv_C": _r(inp.trv_temp_C, 2),
                        "mix_w_internal": (
                            _r(_mix_int, 2) if _mix_int is not None else None
                        ),
                        "mix_w_external": (
                            _r(_mix_ext, 2) if _mix_ext is not None else None
                        ),
                        "meas_blend_C": _r(meas_now, 2),
                        "meas_smooth_C": _r(smoothed, 2),
                        "d_meas_per_s": _r(d_meas, 4),
                    }
                except Exception:
                    pid_dbg = {"mode": "pid"}
            else:
                # Heuristik wie bisher
                # Base mapping via band_far
                bf = max(params.band_far_K, 0.1)
                # Clamp ΔT to [-bf, +bf] and map linearly to [0..100]
                x = max(-bf, min(bf, delta_T))
                percent_base = 100.0 * (x + bf) / (2.0 * bf)

                # Slope EMA
                slope = inp.temp_slope_K_per_min
                if slope is not None:
                    if st.ema_slope is None:
                        st.ema_slope = slope
                    else:
                        # light smoothing of the trend
                        st.ema_slope = 0.6 * st.ema_slope + 0.4 * slope
                s = st.ema_slope if st.ema_slope is not None else 0.0

                # Slope correction: positive slope → reduce percentage
                percent_adj = percent_base + params.slope_gain_per_K_per_min * s

                # With clear heating demand (ΔT >= band_far) fully open
                if delta_T >= params.band_far_K:
                    percent = 100.0
                # With clear overshoot (ΔT <= -band_far) close
                elif delta_T <= -params.band_far_K:
                    percent = 0.0
                else:
                    # Near setpoint: light extra logic
                    if abs(delta_T) <= params.band_near_K:
                        # Rising faster than desired
                        if s >= params.slope_up_K_per_min:
                            percent_adj *= 0.7  # throttle
                        # If temperature does not increase despite demand
                        elif s <= params.slope_down_K_per_min and delta_T > 0:
                            percent_adj = max(percent_adj, 60.0)
                    percent = max(0.0, min(100.0, percent_adj))

    # Percentage smoothing (EMA) - skip for MPC (has internal smoothing via change_penalty)
    mode_lower = params.mode.lower()
    if st.last_percent is None or mode_lower == "mpc":
        smooth = percent
    else:
        a = params.percent_smoothing_alpha
        smooth = (1.0 - a) * st.last_percent + a * percent

    # Hysteresis + rate limit
    too_soon = (now - st.last_update_ts) < params.min_update_interval_s
    # One-shot Bypass: Bei Zieltemperatur-Änderung sofortiges Update erlauben
    target_changed = False
    try:
        cur_t = inp.target_temp_C
        prev_t = st.last_target_C
        if cur_t is not None and prev_t is not None:
            target_changed = abs(float(cur_t) - float(prev_t)) >= 0.05
        st.last_target_C = cur_t if cur_t is not None else st.last_target_C
        if target_changed:
            too_soon = False
            # Bei Ziel-Änderung auch die EMA-Glättung einmalig überspringen
            smooth = percent
    except Exception:
        target_changed = False
    # Bei starker Abweichung Hysterese/Rate-Limit umgehen (fast-close/fast-open)
    force_close = False
    force_open = False
    try:
        if inp.target_temp_C is not None and inp.current_temp_C is not None:
            dT = inp.target_temp_C - inp.current_temp_C
            force_close = dT <= -params.band_far_K
            force_open = dT >= params.band_far_K
    except Exception:
        force_close = False
        force_open = False
    if force_close:
        # Bei Überschwingen sofort reagieren (Rate-Limit außer Kraft)
        too_soon = False
        try:
            # Smoothing überspringen → direkt auf 0% schließen
            smooth = 0.0
        except Exception:
            pass
    if st.last_percent is not None:
        if (
            abs(smooth - st.last_percent) < params.percent_hysteresis_pts
            and not force_close
            and not target_changed
            and not force_open
        ) or (too_soon and not force_open):
            # no change – return previous state
            percent_out = int(round(st.last_percent))
        else:
            percent_out = int(round(smooth))
            st.last_percent = smooth
            st.last_update_ts = now
    else:
        percent_out = int(round(smooth))
        st.last_percent = smooth
        st.last_update_ts = now

    # Generic mapping to setpoint throttling
    # flow_cap_K increases as percent_out decreases (0% → cap_max, 100% → 0K)
    flow_cap_K = params.cap_max_K * (1.0 - (percent_out / 100.0))

    setpoint_eff = None
    if inp.target_temp_C is not None:
        # Only apply throttling on overshoot (current >= target). When demand is present
        # (current < target), do not reduce setpoint to avoid fighting heating demand.
        if delta_T is not None and delta_T <= 0.0:
            setpoint_eff = inp.target_temp_C - flow_cap_K

    # Sonoff mapping: max_open = percent; min_open depends on overshoot/comfort
    min_open = params.sonoff_min_open_default_pct
    if delta_T is not None:
        if delta_T <= -params.band_near_K:
            min_open = 0  # too warm → minimal residual flow
        elif abs(delta_T) <= params.band_near_K and (st.ema_slope or 0.0) > 0:
            min_open = params.sonoff_min_open_default_pct  # gentle hold
    sonoff_max = max(0, min(100, percent_out))
    sonoff_min = max(0, min(sonoff_max, min_open))  # min <= max

    debug = {
        "delta_T": _r(delta_T, 2) if delta_T is not None else None,
        "slope_ema": _r(st.ema_slope, 3),
        "percent_base": (
            None
            if inp.target_temp_C is None or inp.current_temp_C is None
            else _r(percent_base, 2)
        ),
        "percent_raw": _r(percent, 2),
        "percent_smooth": _r(smooth, 2),
        "too_soon": too_soon,
        "target_changed": target_changed,
        "target_prev": _r(getattr(st, "last_target_C", None), 2),
        "target_cur": (
            _r(inp.target_temp_C, 2) if inp.target_temp_C is not None else None
        ),
        "force_open": force_open,
        "force_close": force_close,
    }
    # PID-Debug anhängen, falls vorhanden
    try:
        mode_lower_dbg = params.mode.lower()
        if mode_lower_dbg == "pid" or mode_lower_dbg == "mpc":
            debug["pid"] = (
                pid_dbg
                if isinstance(pid_dbg, dict) and pid_dbg
                else {"mode": mode_lower_dbg}
            )
        else:
            debug["pid"] = {"mode": "heuristic"}
    except Exception:
        pass

    return BalanceOutput(
        valve_percent=sonoff_max,
        flow_cap_K=round(flow_cap_K, 3),
        setpoint_eff_C=round(setpoint_eff, 3) if setpoint_eff is not None else None,
        sonoff_min_open_pct=sonoff_min,
        sonoff_max_open_pct=sonoff_max,
        debug=debug,
    )


def _auto_tune_pid(
    params: BalanceParams,
    st: BalanceState,
    percent: float,
    delta_T: Optional[float],
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
        # Overshoot-Heuristik: Vorzeichenwechsel und Amplitude über Schwellwert
        if st.last_delta_sign is not None and sign != 0 and sign != st.last_delta_sign:
            if abs(delta_T) >= params.overshoot_threshold_K:
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

        # 2) Trägheit: ΔT deutlich > band_near, aber Slope sehr klein -> Ki leicht rauf
        if (
            delta_T > params.band_near_K
            and abs(slope) < params.sluggish_slope_threshold_K_min
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


def reset_balance_state(key: str) -> None:
    """Reset learned/smoothing values for a given room key."""
    if key in _BALANCE_STATES:
        del _BALANCE_STATES[key]


def get_balance_state(key: str) -> Optional[BalanceState]:
    return _BALANCE_STATES.get(key)


def seed_pid_gains(
    key: str,
    kp: Optional[float] = None,
    ki: Optional[float] = None,
    kd: Optional[float] = None,
) -> bool:
    """Pre-seed PID gains for a given state key without overriding existing values.

    Returns True if any value was set.
    """
    st = _BALANCE_STATES.setdefault(key, BalanceState())
    changed = False
    try:
        if kp is not None and st.pid_kp is None:
            st.pid_kp = float(kp)
            changed = True
    except Exception:
        pass
    try:
        if ki is not None and st.pid_ki is None:
            st.pid_ki = float(ki)
            changed = True
    except Exception:
        pass
    try:
        if kd is not None and st.pid_kd is None:
            st.pid_kd = float(kd)
            changed = True
    except Exception:
        pass
    return changed


def reset_mpc_deadzone(key: str, start_calibration: bool = False) -> bool:
    """Reset MPC deadzone estimation for a given state key.

    Args:
        key: State key for the room/TRV
        start_calibration: If True, immediately start active calibration test

    Returns True if deadzone was reset.
    """
    # Ensure state exists so we can always (re)start calibration
    st = _BALANCE_STATES.setdefault(key, BalanceState())

    # Build TRV-level cache key (uid + entity, without temperature bucket)
    try:
        trv_key = key.rsplit(":", 1)[0]
    except Exception:  # noqa: BLE001
        trv_key = key

    # Drop any globally cached deadzone so the upcoming calibration is not short-circuited
    if _DEADZONE_CACHE.pop(trv_key, None) is not None:
        _LOGGER.debug(
            "reset_mpc_deadzone(%s): cleared global deadzone cache entry for %s",
            key,
            trv_key,
        )

    # Also clear stale deadzone values on sibling buckets for the same TRV
    for other_key, other_state in _BALANCE_STATES.items():
        if other_key == key:
            continue
        try:
            other_trv_key = other_key.rsplit(":", 1)[0]
        except Exception:  # noqa: BLE001
            other_trv_key = other_key
        if other_trv_key == trv_key:
            other_state.mpc_deadzone_est = None

    st.mpc_deadzone_est = None
    st.mpc_deadzone_last_log = 0.0
    if start_calibration:
        # Start cooling phase immediately (will be executed on next balance call)
        st.mpc_deadzone_test_cooling = True
        st.mpc_deadzone_test_cooling_start = monotonic()  # Set to current time
        st.mpc_deadzone_test_active = False
        st.mpc_deadzone_test_u = 0.0
        st.mpc_deadzone_test_start_ts = 0.0
        st.mpc_deadzone_test_trv_start = None
        st.mpc_deadzone_last_log = 0.0
        _LOGGER.inf(
            "reset_mpc_deadzone(%s): calibration started, cooling=True, cooling_start=%.1f",
            key,
            st.mpc_deadzone_test_cooling_start,
        )
    else:
        # Just reset, let automatic trigger handle it
        st.mpc_deadzone_test_cooling = False
        st.mpc_deadzone_test_cooling_start = 0.0
        st.mpc_deadzone_test_active = False
        st.mpc_deadzone_test_u = 0.0
        st.mpc_deadzone_test_start_ts = 0.0
        st.mpc_deadzone_test_trv_start = None
        st.mpc_deadzone_last_log = 0.0
        _LOGGER.debug(
            "reset_mpc_deadzone(%s): calibration reseted, not started", key
        )
    return True


def start_mpc_deadzone_calibration(key: str) -> bool:
    """Start active MPC deadzone calibration for a given state key.

    This will reset any existing deadzone and immediately start the test sequence.
    If the state doesn't exist yet, it will be created.

    Returns True if calibration was started.
    """
    result = reset_mpc_deadzone(key, start_calibration=True)
    _LOGGER.debug("start_mpc_deadzone_calibration(%s): result=%s", key, result)
    return result
# --- Persistence helpers --------------------------------------------


def export_states(prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Export internal state to a JSON-serializable dict.

    prefix: if provided, only include keys starting with this prefix.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for k, st in _BALANCE_STATES.items():
        if prefix is not None and not k.startswith(prefix):
            continue
        out[k] = {
            "last_percent": st.last_percent,
            "last_update_ts": st.last_update_ts,
            "ema_slope": st.ema_slope,
            "pid_kp": st.pid_kp,
            "pid_ki": st.pid_ki,
            "pid_kd": st.pid_kd,
            "last_tune_ts": st.last_tune_ts,
            "mpc_gain_est": st.mpc_gain_est,
            "mpc_loss_est": st.mpc_loss_est,
            "mpc_deadzone_est": st.mpc_deadzone_est,
        }
    return out


def import_states(
    data: Dict[str, Dict[str, Any]], prefix_filter: Optional[str] = None
) -> int:
    """Import previously saved states into the module-local cache.

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
            st = BalanceState(
                last_percent=v.get("last_percent"),
                last_update_ts=float(v.get("last_update_ts", 0.0)),
                ema_slope=v.get("ema_slope"),
                pid_kp=v.get("pid_kp"),
                pid_ki=v.get("pid_ki"),
                pid_kd=v.get("pid_kd"),
                last_tune_ts=float(v.get("last_tune_ts", 0.0)),
                mpc_gain_est=v.get("mpc_gain_est"),
                mpc_loss_est=v.get("mpc_loss_est"),
                mpc_deadzone_est=v.get("mpc_deadzone_est"),
            )
            _BALANCE_STATES[str(k)] = st
            count += 1
        except (KeyError, TypeError, ValueError):
            # Ignore malformed entries
            continue
    return count
