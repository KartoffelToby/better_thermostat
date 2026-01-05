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

    clamp_min_pct: float = 0.0
    clamp_max_pct: float = 100.0
    # TPI coefficients like in versatile_thermostat
    coef_int: float = 0.6  # coef_int for internal delta
    coef_ext: float = 0.01  # coef_ext for external delta
    # Thresholds to disable/enable algorithm based on error
    threshold_low: float = 0.0  # re-enable when error < threshold_low
    threshold_high: float = 0.3  # disable when error > threshold_high


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


_TPI_STATES: Dict[str, _TpiState] = {}

_STATE_EXPORT_FIELDS = ("last_percent",)


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
                coerced: int | float
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

    # Simple TPI calculation like in versatile_thermostat
    duty_pct = params.coef_int * error_K
    if inp.outdoor_temp_C is not None:
        delta_ext = float(inp.target_temp_C) - float(inp.outdoor_temp_C)
        duty_pct += params.coef_ext * delta_ext

    # Convert to percentage (0-100)
    duty_pct *= 100.0

    # Apply thresholds: if temperature overshoots (error negative and |error| > threshold_high), disable heating
    if params.threshold_high > 0.0 and error_K < -params.threshold_high:
        duty_pct = 0.0
        debug = {"reason": "threshold_high"}
        return _finalize_output(inp, params, state, now, duty_pct, error_K, debug)

    # If error < threshold_low, re-enable calculation (but since we already calculated, maybe no change)

    debug = {
        "error_K": _round_dbg(error_K),
        "coef_int": _round_dbg(params.coef_int, 3),
        "coef_ext": _round_dbg(params.coef_ext, 3),
        "raw_pct": _round_dbg(duty_pct, 2),
    }

    return _finalize_output(inp, params, state, now, duty_pct, error_K, debug)


def _finalize_output(
    inp: TpiInput,
    params: TpiParams,
    state: _TpiState,
    now: float,
    duty_pct_raw: float,
    error_K: Optional[float],
    debug: Dict[str, Any],
) -> TpiOutput:
    # Clamp
    duty_pct = max(params.clamp_min_pct, min(params.clamp_max_pct, duty_pct_raw))

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
