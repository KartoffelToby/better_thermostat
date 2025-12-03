"""Time Proportional Integrator (TPI) controller with self-learning.

This module is inspired by the TPI logic used in jmcollin78/versatile_thermostat,
adapted to Better Thermostat architecture. It computes a duty cycle for a fixed
cycle duration and exposes rich debug logs for diagnostics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Dict, Mapping, Optional

_LOGGER = logging.getLogger(__name__)


@dataclass
class TpiParams:
    """Parameters for the TPI controller."""

    cycle_duration_s: float = 300.0  # 5 minutes default
    clamp_min_pct: float = 0.0
    clamp_max_pct: float = 100.0
    hysteresis_pct: float = 1.0
    # If true, use relative hysteresis as fraction of previous value (e.g. 0.05 -> 5%)
    hysteresis_relative: bool = False
    hysteresis_rel_frac: float = 0.05
    min_on_s: float = 60.0
    min_off_s: float = 60.0
    # learning / smoothing parameters
    slope_alpha: float = 0.2  # EMA weight for slope
    gain_alpha: float = 0.1  # EMA weight when updating gain_est
    loss_alpha: float = 0.15  # EMA weight when updating loss_est
    base_gain_pct_per_K: float = 10.0  # initial mapping from error(K) -> %
    gain_min_pct_per_K: float = 1.0
    gain_max_pct_per_K: float = 100.0
    outdoor_gain_scale: float = 1.0  # multiplicative factor relative to delta_env
    min_effective_percent_floor: float = 5.0
    min_effective_percent_max: float = 20.0
    min_eff_increase_step: float = 1.0
    min_eff_decrease_step: float = 0.5
    # how many consecutive cycles of "little slope while heating" before increasing min_effective_percent
    min_eff_increase_window: int = 3
    # minimum dT to consider for gain learning (K)
    min_dT_for_gain_learning_K: float = 0.1
    # minimum time between TPI output updates (seconds)
    update_interval_s: float = 300.0


@dataclass
class TpiInput:
    key: str
    current_temp_C: Optional[float]
    target_temp_C: Optional[float]
    outdoor_temp_C: Optional[float] = None
    window_open: bool = False
    heating_allowed: bool = True
    bt_name: Optional[str] = None
    entity_id: Optional[str] = None


@dataclass
class TpiOutput:
    duty_cycle_pct: float
    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _TpiState:
    last_percent: Optional[float] = None
    last_update_ts: float = 0.0
    last_temp: Optional[float] = None
    last_time: float = 0.0
    ema_slope: Optional[float] = None  # K/min
    gain_est: Optional[float] = None  # pct per K
    loss_est: Optional[float] = None  # K/min (cooling without heating)
    min_effective_percent: Optional[float] = None
    # small counter to avoid flapping min_effective_percent
    _min_eff_bad_cycles: int = 0


_TPI_STATES: Dict[str, _TpiState] = {}

_STATE_EXPORT_FIELDS = (
    "last_percent",
    "ema_slope",
    "gain_est",
    "loss_est",
    "last_temp",
    "min_effective_percent",
)


def export_tpi_state_map(prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Return a serializable mapping of TPI states, optionally filtered by key prefix."""

    exported: Dict[str, Dict[str, Any]] = {}
    for key, state in _TPI_STATES.items():
        if prefix is not None and not key.startswith(prefix):
            continue
        payload: Dict[str, Any] = {}
        for attr in _STATE_EXPORT_FIELDS:
            value = getattr(state, attr, None)
            if value is None:
                continue
            payload[attr] = value
        if payload:
            exported[key] = payload
    return exported


def import_tpi_state_map(state_map: Mapping[str, Mapping[str, Any]]) -> None:
    """Hydrate TPI states from a previously exported mapping."""

    for key, payload in state_map.items():
        if not isinstance(payload, Mapping):
            continue
        state = _TPI_STATES.setdefault(key, _TpiState())
        for attr in _STATE_EXPORT_FIELDS:
            if attr not in payload:
                continue
            value = payload[attr]
            if value is None:
                setattr(state, attr, None)
                continue
            try:
                if attr in ("dead_zone_hits",):
                    coerced = int(value)
                else:
                    coerced = float(value)
            except (TypeError, ValueError):
                continue
            setattr(state, attr, coerced)


def _round_dbg(v: Any, d: int = 3) -> Any:
    try:
        return round(float(v), d)
    except (TypeError, ValueError):
        return v


def compute_tpi(inp: TpiInput, params: TpiParams) -> Optional[TpiOutput]:
    """Compute TPI duty cycle and on/off durations.

    Returns None if inputs are insufficient. Emits extensive debug logs.
    """

    now = monotonic()
    state = _TPI_STATES.setdefault(inp.key, _TpiState())

    name = inp.bt_name or "BT"
    entity = inp.entity_id or "unknown"

    _LOGGER.debug(
        "better_thermostat %s: TPI input (%s) target=%s current=%s outdoor=%s window_open=%s allowed=%s last_percent=%s",
        name,
        entity,
        _round_dbg(inp.target_temp_C),
        _round_dbg(inp.current_temp_C),
        _round_dbg(inp.outdoor_temp_C),
        inp.window_open,
        inp.heating_allowed,
        _round_dbg(state.last_percent, 2),
    )

    if not inp.heating_allowed or inp.window_open:
        duty_pct = 0.0
        debug: Dict[str, Any] = {"reason": "blocked"}
        return _finalize_output(inp, params, state, now, duty_pct, None, debug)

    if inp.current_temp_C is None or inp.target_temp_C is None:
        # Reuse last percent if available
        duty_pct = state.last_percent if state.last_percent is not None else 0.0
        debug = {"reason": "missing_temps"}
        return _finalize_output(inp, params, state, now, duty_pct, None, debug)

    # Error in Kelvin
    error_K = float(inp.target_temp_C) - float(inp.current_temp_C)

    # Estimate gain if we have slope from last cycle
    raw_pct = state.last_percent if state.last_percent is not None else 0.0
    slope_K_per_min = _estimate_slope(state, now, inp.current_temp_C, raw_pct, params)
    gain_est = state.gain_est if state.gain_est is not None else params.base_gain_pct_per_K

    # Outdoor influence: scale gain relative to environmental delta
    outdoor_influence = None
    if inp.outdoor_temp_C is not None and inp.target_temp_C is not None:
        delta_env = max(float(inp.target_temp_C) - float(inp.outdoor_temp_C), 0.1)
        # gentler scaling: clamp and modest slope; keep impact bounded
        outdoor_influence = max(0.6, min(1.6, 1.0 + (delta_env - 10.0) * 0.03))
        gain_est = gain_est * outdoor_influence

    # Primary proportional mapping
    duty_pct = max(0.0, error_K * gain_est)

    # Loss compensation: if we know cooling rate, offset the duty upwards (bounded)
    if state.loss_est and state.loss_est > 0.0:
        loss_over_cycle_K = state.loss_est * (params.cycle_duration_s / 60.0)
        duty_pct += loss_over_cycle_K * gain_est

    # Minimum effective percent floor
    min_eff = state.min_effective_percent
    if min_eff is None:
        min_eff = params.min_effective_percent_floor
    # Apply min effective only when we propose to heat
    if duty_pct > 0.0:
        duty_pct = max(duty_pct, min_eff)

    debug = {
        "error_K": _round_dbg(error_K),
        "ema_slope_K_per_min": _round_dbg(state.ema_slope, 4),
        "slope_est_K_per_min": _round_dbg(slope_K_per_min, 4),
        "gain_est_pct_per_K": _round_dbg(gain_est, 3),
        "loss_est_K_per_min": _round_dbg(state.loss_est, 4),
        "outdoor_influence": _round_dbg(outdoor_influence, 3),
        "raw_pct": _round_dbg(duty_pct, 2),
        "min_effective_percent": _round_dbg(min_eff, 2),
    }
    # structured learning debug
    debug["learn"] = {
        "gain_est": _round_dbg(state.gain_est, 3),
        "loss_est": _round_dbg(state.loss_est, 4),
        "min_eff_counter": getattr(state, "_min_eff_bad_cycles", 0),
    }

    return _finalize_output(inp, params, state, now, duty_pct, error_K, debug)


def _estimate_slope(
    state: _TpiState,
    now: float,
    cur_temp: Optional[float],
    last_pct: float,
    params: TpiParams,
) -> Optional[float]:
    """Estimate room temperature slope (K/min) and update EMA and learning terms."""

    if cur_temp is None:
        return state.ema_slope

    if state.last_temp is None:
        state.last_temp = float(cur_temp)
        state.last_time = now
        return state.ema_slope

    dt_s = now - state.last_time
    # Only update slope if at least 30 seconds have passed (avoid noise from frequent calls)
    if dt_s < 30.0:
        return state.ema_slope

    dT_K = float(cur_temp) - float(state.last_temp)
    slope = (dT_K / dt_s) * 60.0  # K per minute

    # Update EMA slope
    if state.ema_slope is None:
        state.ema_slope = slope
    else:
        alpha = params.slope_alpha
        state.ema_slope = (1 - alpha) * state.ema_slope + alpha * slope

    # Update gain and loss estimates - only if we have meaningful data
    if dt_s >= 60.0 and abs(dT_K) >= params.min_dT_for_gain_learning_K:
        # Learn gain_est only when heating has produced a measurable positive slope
        if last_pct > 5.0 and slope > 0.0:
            # gain_est as pct per K; require dT sufficient to be above noise
            gain = last_pct / max(abs(dT_K), params.min_dT_for_gain_learning_K)
            # Bound the raw new estimate to avoid explosion
            gain = max(params.gain_min_pct_per_K, min(params.gain_max_pct_per_K, gain))
            if state.gain_est is None:
                state.gain_est = gain
            else:
                alpha = params.gain_alpha
                # anti-windup: limit relative change per update (e.g. +/-50% of old)
                max_rel_change = 0.5
                new_est = (1 - alpha) * state.gain_est + alpha * gain
                # clamp relative move
                lower = state.gain_est * (1 - max_rel_change)
                upper = state.gain_est * (1 + max_rel_change)
                state.gain_est = max(lower, min(upper, new_est))
        elif last_pct < 5.0 and slope < -0.01:
            # loss_est: only learn cooling rate when not heating and temp falling
            loss = -slope  # K/min cooling rate
            if state.loss_est is None:
                state.loss_est = loss
            else:
                alpha = params.loss_alpha
                state.loss_est = (1 - alpha) * state.loss_est + alpha * loss

    # Use a windowed approach to avoid oscillatory increases of minimum effective percent
    if last_pct > 10.0 and slope < 0.02:
        state._min_eff_bad_cycles = getattr(state, "_min_eff_bad_cycles", 0) + 1
        if state._min_eff_bad_cycles >= params.min_eff_increase_window:
            base = state.min_effective_percent or 0.0
            new_min = min(base + params.min_eff_increase_step,
                          params.min_effective_percent_max)
            state.min_effective_percent = new_min
            state._min_eff_bad_cycles = 0
    elif slope > 0.03:
        # Stronger heating response - gently reduce minimum
        state._min_eff_bad_cycles = 0
        if state.min_effective_percent is None:
            state.min_effective_percent = 0.0
        else:
            state.min_effective_percent = max(
                state.min_effective_percent - params.min_eff_decrease_step, 0.0)

    state.last_temp = float(cur_temp)
    state.last_time = now
    return state.ema_slope


def _finalize_output(
    inp: TpiInput,
    params: TpiParams,
    state: _TpiState,
    now: float,
    duty_pct_raw: float,
    error_K: Optional[float],
    debug: Dict[str, Any],
) -> TpiOutput:
    # Check minimum time between output updates (configurable)
    if state.last_update_ts is not None and now - state.last_update_ts < params.update_interval_s:
        debug["reason"] = "too_soon"
        return TpiOutput(duty_cycle_pct=state.last_percent or 0.0, debug=debug)

    # Clamp and hysteresis
    duty_pct = max(params.clamp_min_pct, min(params.clamp_max_pct, duty_pct_raw))

    # Apply hysteresis on small changes
    prev = state.last_percent or 0.0
    if params.hysteresis_relative and prev != 0.0:
        rel_thresh = abs(prev) * params.hysteresis_rel_frac
        if abs(duty_pct - prev) < rel_thresh:
            duty_pct = prev
    else:
        if abs(duty_pct - prev) < params.hysteresis_pct:
            duty_pct = prev

    state.last_percent = duty_pct
    state.last_update_ts = now

    debug.update(
        {
            "duty_cycle_pct": _round_dbg(duty_pct, 2),
            "error_K": _round_dbg(error_K) if error_K is not None else None,
        }
    )

    name = inp.bt_name or "BT"
    entity = inp.entity_id or "unknown"
    _LOGGER.debug(
        "better_thermostat %s: TPI output (%s) duty=%s%% debug=%s",
        name,
        entity,
        _round_dbg(duty_pct, 2),
        debug,
    )

    return TpiOutput(duty_cycle_pct=duty_pct, debug=debug)


def build_tpi_key(bt, entity_id: str) -> str:
    """Return a stable key for TPI state tracking (similar to MPC)."""

    try:
        target = bt.bt_target_temp
        bucket = (
            f"t{round(float(target) * 2.0) / 2.0:.1f}"
            if isinstance(target, (int, float))
            else "tunknown"
        )
    except (TypeError, ValueError):
        bucket = "tunknown"

    uid = getattr(bt, "unique_id", None) or getattr(bt, "_unique_id", "bt")
    return f"{uid}:{entity_id}:{bucket}"
