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
    tolerance_K: float = 0.15
    clamp_min_pct: float = 0.0
    clamp_max_pct: float = 100.0
    hysteresis_pct: float = 2.0
    min_on_s: float = 60.0
    min_off_s: float = 60.0
    learn_rate: float = 0.1  # EMA alpha
    base_gain_pct_per_K: float = 10.0  # initial mapping from error(K) -> %
    outdoor_gain_scale: float = 1.0  # multiplicative factor relative to delta_env
    min_effective_percent_floor: float = 5.0


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
    dead_zone_hits: int = 0


_TPI_STATES: Dict[str, _TpiState] = {}

_STATE_EXPORT_FIELDS = (
    "last_percent",
    "ema_slope",
    "gain_est",
    "loss_est",
    "last_temp",
    "min_effective_percent",
    "dead_zone_hits",
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

    # Deadzone: small band around target
    if abs(error_K) <= params.tolerance_K:
        state.dead_zone_hits = min(state.dead_zone_hits + 1, 1000)
        # decay last percent
        duty_pct = max(0.0, (state.last_percent or 0.0) - params.hysteresis_pct)
        debug = {"reason": "deadzone", "dead_zone_hits": state.dead_zone_hits}
        return _finalize_output(inp, params, state, now, duty_pct, error_K, debug)
    else:
        state.dead_zone_hits = max(state.dead_zone_hits - 1, 0)

    # Estimate gain if we have slope from last cycle
    raw_pct = state.last_percent if state.last_percent is not None else 0.0
    slope_K_per_min = _estimate_slope(state, now, inp.current_temp_C, raw_pct)
    gain_est = (
        state.gain_est if state.gain_est is not None else params.base_gain_pct_per_K
    )

    # Outdoor influence: scale gain relative to environmental delta
    outdoor_influence = None
    if inp.outdoor_temp_C is not None and inp.target_temp_C is not None:
        delta_env = max(float(inp.target_temp_C) - float(inp.outdoor_temp_C), 0.1)
        # Scale factor: 1.0 at delta_env=10K, up to 2.0 at delta_env=30K, min 0.5
        outdoor_influence = max(0.5, min(2.0, 1.0 + (delta_env - 10.0) * 0.05))
        gain_est = gain_est * outdoor_influence

    # Primary proportional mapping
    duty_pct = max(0.0, error_K * gain_est)

    # Loss compensation: if we know cooling rate, offset the duty upwards
    if state.loss_est and state.loss_est > 0.0:
        # convert loss (K/min) over cycle to equivalent % using gain
        loss_over_cycle_K = state.loss_est * (params.cycle_duration_s / 60.0)
        duty_pct += loss_over_cycle_K * gain_est

    # Minimum effective percent floor
    min_eff = state.min_effective_percent
    if min_eff is None:
        min_eff = params.min_effective_percent_floor
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
        "dead_zone_hits": state.dead_zone_hits,
        "min_effective_percent": _round_dbg(min_eff, 2),
    }

    return _finalize_output(inp, params, state, now, duty_pct, error_K, debug)


def _estimate_slope(
    state: _TpiState, now: float, cur_temp: Optional[float], last_pct: float
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
        alpha = 0.2  # 20% new, 80% old - moderate smoothing
        state.ema_slope = (1 - alpha) * state.ema_slope + alpha * slope

    # Update gain and loss estimates - only if we have meaningful data
    if dt_s >= 60.0 and abs(dT_K) >= 0.05:  # At least 1 minute and 0.05K change
        if last_pct > 5.0 and slope > 0.0:
            # gain_est as pct per K: only learn when heating significantly and temp rising
            # Simplified: duty_cycle_pct / temp_change_K gives pct needed per K error
            gain = last_pct / max(dT_K, 0.01)
            if state.gain_est is None:
                state.gain_est = gain
            else:
                # Use consistent EMA alpha
                alpha = 0.1  # 10% new, 90% old
                state.gain_est = (1 - alpha) * state.gain_est + alpha * gain
        elif last_pct < 5.0 and slope < -0.01:
            # loss_est: only learn cooling rate when not heating and temp falling
            loss = -slope  # K/min cooling rate
            if state.loss_est is None:
                state.loss_est = loss
            else:
                alpha = 0.15  # slightly faster than gain learning
                state.loss_est = (1 - alpha) * state.loss_est + alpha * loss

    # Update min effective percent when heating yields almost no slope
    if last_pct > 10.0 and slope < 0.02:
        # Heating but barely warming - increase minimum duty cycle
        base = state.min_effective_percent or 0.0
        state.min_effective_percent = min(max(base + 1.0, 0.0), 30.0)
    elif slope > 0.1:
        # Strong heating response - can reduce minimum
        if state.min_effective_percent is None:
            state.min_effective_percent = 0.0
        else:
            state.min_effective_percent = max(state.min_effective_percent - 0.5, 0.0)

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
    # Clamp and hysteresis
    duty_pct = max(params.clamp_min_pct, min(params.clamp_max_pct, duty_pct_raw))

    # Apply hysteresis on small changes
    prev = state.last_percent or 0.0
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
