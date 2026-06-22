"""Time Proportional Integrator (TPI) controller with self-learning.

This module is inspired by the TPI logic used in jmcollin78/versatile_thermostat,
adapted to Better Thermostat architecture. It computes a duty cycle for a fixed
cycle duration and exposes rich debug logs for diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
import logging
import math
from time import monotonic
from typing import TYPE_CHECKING, Any

from custom_components.better_thermostat.core.calibrator import CalibratorHealth

if TYPE_CHECKING:
    from ...climate import BetterThermostat

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
    """Input parameters for TPI calibration calculation."""

    key: str
    current_temp_C: float | None
    target_temp_C: float | None
    outdoor_temp_C: float | None = None
    window_open: bool = False
    heating_allowed: bool = True
    bt_name: str | None = None
    entity_id: str | None = None


@dataclass
class TpiOutput:
    """Output result from TPI calibration calculation."""

    duty_cycle_pct: float
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass
class _TpiState:
    last_percent: float | None = None
    last_update_ts: float = 0.0


def sanitize_tpi_state(state: _TpiState) -> tuple[_TpiState, CalibratorHealth]:
    """Self-heal a poisoned TPI state before computing.

    TPI carries no learned model — a non-finite remnant is simply
    dropped and the duty cycle derives from live readings again.
    """
    for f in fields(state):
        value = getattr(state, f.name)
        if isinstance(value, float) and not math.isfinite(value):
            return _TpiState(), CalibratorHealth.NON_FINITE
    return state, CalibratorHealth.HEALTHY


# Public alias so callers can reference the state type without
# importing a private name.
TpiState = _TpiState


def _round_dbg(v: float | int | None, d: int = 3) -> float | int | None:
    if v is None:
        return None
    try:
        return round(float(v), d)
    except TypeError, ValueError:
        return v


def compute_tpi(
    inp: TpiInput, params: TpiParams, *, state: _TpiState
) -> tuple[TpiOutput | None, _TpiState]:
    """Compute TPI duty cycle and on/off durations.

    Parameters
    ----------
    inp:
        Current measurements and context.
    params:
        Controller configuration.
    state:
        Mutable controller state, owned by the caller (typically read from
        and written back to the ``StateManager``).  It is mutated in place
        and returned.

    Returns
    -------
    tuple[TpiOutput | None, _TpiState]
        The duty-cycle recommendation (or ``None`` on early exit) **and**
        the updated state object.
    """
    now = monotonic()

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
        debug: dict[str, Any] = {"reason": "blocked"}
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
    error_K: float | None,
    debug: dict[str, Any],
) -> tuple[TpiOutput, _TpiState]:
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

    return TpiOutput(duty_cycle_pct=duty_pct, debug=debug), state


def build_tpi_key(bt: BetterThermostat, entity_id: str) -> str:
    """Return a stable key for TPI state tracking (similar to MPC)."""

    try:
        target = bt.bt_target_temp
        bucket = (
            f"t{round(float(target) * 2.0) / 2.0:.1f}"
            if isinstance(target, (int, float))
            else "tunknown"
        )
    except TypeError, ValueError:
        bucket = "tunknown"

    uid = getattr(bt, "unique_id", None) or getattr(bt, "_unique_id", "bt")
    return f"{uid}:{entity_id}:{bucket}"
