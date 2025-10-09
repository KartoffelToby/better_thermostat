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

Integration:
- controlling.convert_outbound_states() can call `compute_balance(...)` and depending
    on device either use `set_valve(percent)` or `set_temperature(setpoint_eff)`.
- For Sonoff: write min/max open via a dedicated adapter implementation if available.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from time import monotonic


# --- Default Parameter -----------------------------------------------

@dataclass
class BalanceParams:
    # Near setpoint band where we gently throttle/refine (Kelvin)
    band_near_K: float = 0.3
    # Far band beyond which fully open/close is likely (Kelvin)
    band_far_K: float = 0.5
    # Max setpoint reduction for generic devices (Kelvin)
    cap_max_K: float = 0.8
    # Slope thresholds (K/min)
    slope_up_K_per_min: float = 0.02  # fast rising → throttle more
    slope_down_K_per_min: float = -0.01  # falling while demand → open more
    # Slope gain (percentage points per K/min, negative means positive slope reduces %)
    slope_gain_per_K_per_min: float = -1000.0  # 0.02 K/min → ~ -20%
    # Smoothing and hysteresis
    percent_smoothing_alpha: float = 0.3  # EMA
    percent_hysteresis_pts: float = 3.0   # minimum change in %-points
    min_update_interval_s: float = 120.0  # minimum time between updates
    # Sonoff minimum opening (comfort/flow noise)
    sonoff_min_open_default_pct: int = 5


# --- I/O and state types ---------------------------------------------

@dataclass
class BalanceInput:
    key: str  # z.B. Entity-ID
    target_temp_C: Optional[float]
    current_temp_C: Optional[float]
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


# Module-local storage
_BALANCE_STATES: Dict[str, BalanceState] = {}


# --- Core computation -------------------------------------------------

def compute_balance(inp: BalanceInput, params: BalanceParams = BalanceParams()) -> BalanceOutput:
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

    # Percentage smoothing (EMA)
    if st.last_percent is None:
        smooth = percent
    else:
        a = params.percent_smoothing_alpha
        smooth = (1.0 - a) * st.last_percent + a * percent

    # Hysteresis + rate limit
    too_soon = (now - st.last_update_ts) < params.min_update_interval_s
    if st.last_percent is not None:
        if abs(smooth - st.last_percent) < params.percent_hysteresis_pts or too_soon:
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
        # Only act when demand present; otherwise neutral
        if (delta_T or 0.0) >= 0.0:
            setpoint_eff = max(
                inp.target_temp_C - flow_cap_K,
                inp.target_temp_C - params.cap_max_K,
            )
        else:
            # On overshoot: stronger throttling is ok
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
        "delta_T": delta_T,
        "slope_ema": st.ema_slope,
        "percent_base": None if inp.target_temp_C is None or inp.current_temp_C is None else percent_base,
        "percent_raw": percent,
        "percent_smooth": smooth,
        "too_soon": too_soon,
    }

    return BalanceOutput(
        valve_percent=sonoff_max,
        flow_cap_K=round(flow_cap_K, 3),
        setpoint_eff_C=round(setpoint_eff, 3) if setpoint_eff is not None else None,
        sonoff_min_open_pct=sonoff_min,
        sonoff_max_open_pct=sonoff_max,
        debug=debug,
    )


def reset_balance_state(key: str) -> None:
    """Reset learned/smoothing values for a given room key."""
    if key in _BALANCE_STATES:
        del _BALANCE_STATES[key]


def get_balance_state(key: str) -> Optional[BalanceState]:
    return _BALANCE_STATES.get(key)
