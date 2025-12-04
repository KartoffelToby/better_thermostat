"""Lightweight MPC helper independent from balance logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Dict, Mapping, Optional, Tuple
import math


_LOGGER = logging.getLogger(__name__)


# MPC operates on fixed 5-minute steps and a 12-step horizon.
MPC_STEP_SECONDS = 300.0
MPC_HORIZON_STEPS = 12


@dataclass
class MpcParams:
    """Configuration for the predictive controller."""

    cap_max_K: float = 0.8
    percent_hysteresis_pts: float = 0.5
    min_update_interval_s: float = 60.0
    mpc_thermal_gain: float = 0.25
    mpc_loss_coeff: float = 0.01
    mpc_control_penalty = 0.00005
    mpc_change_penalty = 0.02
    mpc_adapt: bool = True
    mpc_gain_min: float = 0.005
    mpc_gain_max: float = 0.5
    mpc_loss_min: float = 0.0
    mpc_loss_max: float = 0.05
    mpc_adapt_alpha: float = 0.1
    deadzone_threshold_pct: float = 20.0
    deadzone_temp_delta_K: float = 0.1
    deadzone_time_s: float = 300.0
    deadzone_hits_required: int = 3
    deadzone_raise_pct: float = 2.0
    deadzone_decay_pct: float = 1.0


@dataclass
class MpcInput:
    key: str
    target_temp_C: Optional[float]
    current_temp_C: Optional[float]
    trv_temp_C: Optional[float] = None
    tolerance_K: float = 0.0
    temp_slope_K_per_min: Optional[float] = None
    window_open: bool = False
    heating_allowed: bool = True
    bt_name: Optional[str] = None
    entity_id: Optional[str] = None


@dataclass
class MpcOutput:
    valve_percent: int
    flow_cap_K: float
    setpoint_eff_C: Optional[float]
    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _MpcState:
    last_percent: Optional[float] = None
    last_update_ts: float = 0.0
    last_target_C: Optional[float] = None
    ema_slope: Optional[float] = None
    gain_est: Optional[float] = None
    loss_est: Optional[float] = None
    last_temp: Optional[float] = None
    last_time: float = 0.0
    last_trv_temp: Optional[float] = None
    last_trv_temp_ts: float = 0.0
    dead_zone_hits: int = 0
    min_effective_percent: Optional[float] = None


_MPC_STATES: Dict[str, _MpcState] = {}

_STATE_EXPORT_FIELDS = (
    "last_percent",
    "last_target_C",
    "ema_slope",
    "gain_est",
    "loss_est",
    "last_temp",
    "last_trv_temp",
    "min_effective_percent",
    "dead_zone_hits",
)


def _serialize_state(state: _MpcState) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for attr in _STATE_EXPORT_FIELDS:
        value = getattr(state, attr, None)
        if value is None:
            continue
        payload[attr] = value
    return payload


def export_mpc_state_map(prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Return a serializable mapping of MPC states, optionally filtered by key prefix."""

    exported: Dict[str, Dict[str, Any]] = {}
    for key, state in _MPC_STATES.items():
        if prefix is not None and not key.startswith(prefix):
            continue
        payload = _serialize_state(state)
        if payload:
            exported[key] = payload
    return exported


def import_mpc_state_map(state_map: Mapping[str, Mapping[str, Any]]) -> None:
    """Hydrate MPC states from a previously exported mapping."""

    for key, payload in state_map.items():
        if not isinstance(payload, Mapping):
            continue
        state = _MPC_STATES.setdefault(key, _MpcState())
        for attr in _STATE_EXPORT_FIELDS:
            if attr not in payload:
                continue
            value = payload[attr]
            if value is None:
                setattr(state, attr, None)
                continue
            try:
                if attr == "dead_zone_hits":
                    coerced = int(value)
                else:
                    coerced = float(value)
            except (TypeError, ValueError):
                continue
            setattr(state, attr, coerced)


def _split_mpc_key(key: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    try:
        uid, entity, bucket = key.split(":", 2)
        return uid, entity, bucket
    except ValueError:
        return None, None, None


def _seed_state_from_siblings(key: str, state: _MpcState) -> None:
    if state.min_effective_percent is not None:
        return
    uid, entity, _ = _split_mpc_key(key)
    if not uid or not entity:
        return
    for other_key, other_state in _MPC_STATES.items():
        if other_key == key:
            continue
        ouid, oentity, _ = _split_mpc_key(other_key)
        if ouid == uid and oentity == entity:
            if other_state.min_effective_percent is not None:
                state.min_effective_percent = other_state.min_effective_percent
                return


def build_mpc_key(bt, entity_id: str) -> str:
    """Return a stable key for MPC state tracking."""

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


def _round_for_debug(value: Any, digits: int = 3) -> Any:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def compute_mpc(inp: MpcInput, params: MpcParams) -> Optional[MpcOutput]:
    """Run the predictive controller and emit a valve recommendation."""

    now = monotonic()
    state = _MPC_STATES.setdefault(inp.key, _MpcState())
    _seed_state_from_siblings(inp.key, state)

    extra_debug: Dict[str, Any] = {}
    name = inp.bt_name or "BT"
    entity = inp.entity_id or "unknown"

    _LOGGER.debug(
        "better_thermostat %s: MPC input (%s) target=%s current=%s trv=%s slope=%s window_open=%s allowed=%s last_percent=%s key=%s",
        name,
        entity,
        _round_for_debug(inp.target_temp_C, 3),
        _round_for_debug(inp.current_temp_C, 3),
        _round_for_debug(inp.trv_temp_C, 3),
        _round_for_debug(inp.temp_slope_K_per_min, 4),
        inp.window_open,
        inp.heating_allowed,
        _round_for_debug(state.last_percent, 2),
        inp.key,
    )

    initial_delta_t: Optional[float] = None

    if not inp.heating_allowed or inp.window_open:
        percent = 0.0
        delta_t = None
        _LOGGER.debug(
            "better_thermostat %s: MPC skip heating (%s) window_open=%s heating_allowed=%s",
            name,
            entity,
            inp.window_open,
            inp.heating_allowed,
        )
    else:
        if inp.target_temp_C is None or inp.current_temp_C is None:
            percent = state.last_percent if state.last_percent is not None else 0.0
            delta_t = None
            _LOGGER.debug(
                "better_thermostat %s: MPC missing temps (%s) reusing last_percent=%s",
                name,
                entity,
                _round_for_debug(percent, 2),
            )
        else:
            delta_t = inp.target_temp_C - inp.current_temp_C
            initial_delta_t = delta_t
            if delta_t <= -0.3:
                percent = 0.0
                extra_debug = {}
                _LOGGER.debug(
                    "better_thermostat %s: MPC skip heating due to negative error (%s) delta_T=%s <= -0.3",
                    name,
                    entity,
                    _round_for_debug(delta_t, 3),
                )
            else:
                percent, mpc_debug = _compute_predictive_percent(
                    inp, params, state, now, delta_t
                )
                extra_debug = mpc_debug
                _LOGGER.debug(
                    "better_thermostat %s: MPC raw output (%s) percent=%s delta_T=%s debug=%s",
                    name,
                    entity,
                    _round_for_debug(percent, 2),
                    _round_for_debug(delta_t, 3),
                    mpc_debug,
                )

    percent = max(0.0, min(100.0, percent))
    prev_percent = state.last_percent

    percent_out, debug, delta_t = _post_process_percent(
        inp=inp,
        params=params,
        state=state,
        now=now,
        raw_percent=percent,
        delta_t=delta_t,
    )

    debug.update(extra_debug)

    flow_cap = params.cap_max_K * (1.0 - (percent_out / 100.0))
    setpoint_eff = None
    if inp.target_temp_C is not None and delta_t is not None and delta_t <= 0.0:
        setpoint_eff = inp.target_temp_C - flow_cap

    debug.update(
        {
            "percent_out": percent_out,
            "flow_cap_K": _round_for_debug(flow_cap, 3),
            "setpoint_eff_C": (
                _round_for_debug(setpoint_eff, 3) if setpoint_eff is not None else None
            ),
        }
    )

    summary_delta = delta_t if delta_t is not None else initial_delta_t
    min_eff = state.min_effective_percent
    summary_gain = extra_debug.get("mpc_gain")
    summary_loss = extra_debug.get("mpc_loss")
    summary_horizon = extra_debug.get("mpc_horizon")
    summary_eval = extra_debug.get("mpc_eval_count")
    summary_cost = extra_debug.get("mpc_cost")

    _LOGGER.debug(
        "better_thermostat %s: mpc calibration for %s: e0=%sK gain=%s loss=%s horizon=%s | raw=%s%% out=%s%% min_eff=%s%% last=%s%% dead_hits=%s eval=%s cost=%s flow_cap=%sK",
        name,
        entity,
        _round_for_debug(summary_delta, 3),
        _round_for_debug(summary_gain, 4),
        _round_for_debug(summary_loss, 4),
        summary_horizon,
        _round_for_debug(percent, 2),
        percent_out,
        _round_for_debug(min_eff, 2) if min_eff is not None else None,
        _round_for_debug(prev_percent, 2),
        state.dead_zone_hits,
        summary_eval,
        _round_for_debug(summary_cost, 6),
        _round_for_debug(flow_cap, 3),
    )

    return MpcOutput(
        valve_percent=percent_out,
        flow_cap_K=round(flow_cap, 3),
        setpoint_eff_C=round(setpoint_eff, 3) if setpoint_eff is not None else None,
        debug=debug,
    )


def _compute_predictive_percent(
    inp: MpcInput, params: MpcParams, state: _MpcState, now: float, delta_t: float
) -> Tuple[float, Dict[str, Any]]:
    """Core MPC minimisation routine.

    Overhauled to use a physically consistent temperature-forward model:
    - gain and loss are treated as °C/min and converted to °C/step
    - temperature is simulated forward (°C) rather than multiplying the error
    - quadratic cost (sum of squared errors) is used
    - coarse -> fine candidate search to reduce evals
    - adaptation uses EMA but in physical units (°C/min)
    """

    # Defensive checks
    if inp.current_temp_C is None or inp.target_temp_C is None:
        return 0.0, {"error": "missing temps"}

    # Convert constants & params (use existing param names for backward compatibility)
    step_s = float(getattr(params, "mpc_step_s", MPC_STEP_SECONDS))
    step_minutes = step_s / 60.0
    horizon = int(getattr(params, "mpc_horizon_steps", MPC_HORIZON_STEPS))

    # Initialize estimates if missing
    if params.mpc_adapt:
        if state.gain_est is None:
            state.gain_est = params.mpc_thermal_gain
        if state.loss_est is None:
            state.loss_est = params.mpc_loss_coeff

    # Time since last measurement for adaptation
    dt_last = now - state.last_time if state.last_time > 0 else 0.0

    # ---- ADAPTATION (EMA) in physical units (°C/min) ----
    if (
        params.mpc_adapt
        and state.last_temp is not None
        and inp.current_temp_C is not None
        and inp.target_temp_C is not None
        and dt_last > 1e-6
    ):
        try:
            # previous and current error relative to setpoint
            e_prev = inp.target_temp_C - state.last_temp
            e_now = inp.target_temp_C - inp.current_temp_C
            last_percent = state.last_percent if state.last_percent is not None else 0.0
            u_last = max(0.0, min(100.0, last_percent)) / 100.0

            # observed temperature change (°C) between samples
            delta_T = inp.current_temp_C - state.last_temp
            dt_min = dt_last / 60.0  # minutes

            # estimate rate °C/min
            observed_rate = delta_T / dt_min if dt_min > 0 else 0.0

            if u_last > 0.01:
                # attempt to estimate gain as °C/min at 100% (guarded)
                gain_candidate = observed_rate / u_last
                if gain_candidate >= 0.0 and gain_candidate < params.mpc_gain_max * 10:
                    state.gain_est = (
                        (1.0 - params.mpc_adapt_alpha) * state.gain_est
                        + params.mpc_adapt_alpha * gain_candidate
                    )
                else:
                    # guard: gentle shrink to avoid runaway
                    state.gain_est *= (1.0 - params.mpc_adapt_alpha * 0.5)
            else:
                # with valve closed, learn loss as passive cooling in °C/min
                loss_candidate = max(0.0, -observed_rate)
                if loss_candidate >= 0.0 and loss_candidate < params.mpc_loss_max * 10:
                    state.loss_est = (
                        (1.0 - params.mpc_adapt_alpha) * state.loss_est
                        + params.mpc_adapt_alpha * loss_candidate
                    )
                else:
                    state.loss_est *= (1.0 - params.mpc_adapt_alpha * 0.5)

            # clamp
            state.gain_est = max(params.mpc_gain_min, min(params.mpc_gain_max, state.gain_est))
            state.loss_est = max(params.mpc_loss_min, min(params.mpc_loss_max, state.loss_est))
        except Exception:
            # don't crash adaptation on odd numeric issues
            pass

    # convert to per-step quantities (°C per simulation step)
    gain = state.gain_est if state.gain_est is not None else params.mpc_thermal_gain
    loss = state.loss_est if state.loss_est is not None else params.mpc_loss_coeff
    gain_step = gain * step_minutes
    loss_step = loss * step_minutes

    # cost penalties (normalize u in [0,1])
    control_pen = max(0.0, float(params.mpc_control_penalty))
    change_pen = max(0.0, float(params.mpc_change_penalty))
    last_percent = state.last_percent if state.last_percent is not None else None

    # lag alpha
    lag_tau = float(getattr(params, "mpc_lag_tau_s", 1800.0))
    if lag_tau <= 0:
        lag_tau = 1800.0
    lag_alpha = 1.0 - math.exp(-step_s / lag_tau)

    # candidate search: coarse -> fine (reduces evals while keeping precision)
    best_percent = 0.0
    best_cost = None
    eval_count = 0

    def simulate_cost_for_candidate(u_frac: float) -> float:
        """Simulate forward temperature for constant u_frac (0..1) over horizon and return cost."""
        T = inp.current_temp_C
        cost = 0.0
        for _ in range(horizon):
            heating = gain_step * u_frac
            T_raw = T + heating - loss_step
            T = T + lag_alpha * (T_raw - T)
            e = inp.target_temp_C - T
            cost += e * e
        eval_count_local = 0
        return cost

    # coarse search (0..100 step 10)
    coarse_candidates = list(range(0, 101, 10))
    best_u_coarse = None
    best_cost_coarse = None
    for cand in coarse_candidates:
        u_frac = cand / 100.0
        cost = simulate_cost_for_candidate(u_frac)
        eval_count += horizon
        # penalties
        cost += control_pen * (u_frac * u_frac)
        if last_percent is not None:
            cost += change_pen * abs(u_frac - (last_percent / 100.0))
        if best_cost_coarse is None or cost < best_cost_coarse:
            best_cost_coarse = cost
            best_u_coarse = cand

    # fine search around best coarse ±10% in 2% steps
    best_u_fine = best_u_coarse if best_u_coarse is not None else 0
    best_cost_fine = best_cost_coarse if best_cost_coarse is not None else float("inf")
    fine_lo = max(0, best_u_coarse - 10)
    fine_hi = min(100, best_u_coarse + 10)
    for cand in range(fine_lo, fine_hi + 1, 2):
        u_frac = cand / 100.0
        cost = simulate_cost_for_candidate(u_frac)
        eval_count += horizon
        cost += control_pen * (u_frac * u_frac)
        if last_percent is not None:
            cost += change_pen * abs(u_frac - (last_percent / 100.0))
        if cost < best_cost_fine:
            best_cost_fine = cost
            best_u_fine = cand

    # result before postprocessing
    best_percent = float(best_u_fine)

    # store last estimates
    state.last_temp = inp.current_temp_C
    state.last_time = now

    # build debug
    mpc_debug = {
        "mpc_gain": _round_for_debug(gain, 4),
        "mpc_loss": _round_for_debug(loss, 4),
        "mpc_horizon": horizon,
        "mpc_eval_count": eval_count,
        "mpc_step_minutes": _round_for_debug(step_minutes, 3),
    }

    if best_cost_fine is not None:
        mpc_debug["mpc_cost"] = _round_for_debug(best_cost_fine, 6)

    if last_percent is not None:
        mpc_debug["mpc_last_percent"] = _round_for_debug(last_percent, 2)

    return best_percent, mpc_debug


def _post_process_percent(
    inp: MpcInput,
    params: MpcParams,
    state: _MpcState,
    now: float,
    raw_percent: float,
    delta_t: Optional[float],
) -> tuple[int, Dict[str, Any], Optional[float]]:
    """Apply smoothing, hysteresis, dead-zone detection, and produce debug info."""

    smooth = raw_percent
    target_changed = False
    name = inp.bt_name or "BT"
    entity = inp.entity_id or "unknown"

    if inp.target_temp_C is not None:
        prev_target = state.last_target_C
        if prev_target is not None:
            try:
                target_changed = (
                    abs(float(inp.target_temp_C) - float(prev_target)) >= 0.05
                )
            except (TypeError, ValueError):
                target_changed = False
        state.last_target_C = inp.target_temp_C

    too_soon = (now - state.last_update_ts) < params.min_update_interval_s
    if target_changed:
        too_soon = False

    if inp.target_temp_C is not None and inp.current_temp_C is not None:
        try:
            if delta_t is None:
                delta_t = inp.target_temp_C - inp.current_temp_C
        except (TypeError, ValueError):
            delta_t = None

    min_eff = state.min_effective_percent
    if min_eff is not None and min_eff > 0.0 and smooth > 0.0 and smooth < min_eff:
        smooth = min_eff
        _LOGGER.debug(
            "better_thermostat %s: MPC clamp smooth (%s) to min_effective=%s",
            name,
            entity,
            _round_for_debug(min_eff, 2),
        )

    last_percent = state.last_percent
    if last_percent is not None:
        change = abs(smooth - last_percent)
        if (change < params.percent_hysteresis_pts and not target_changed) or too_soon:
            percent_out = int(round(last_percent))
        else:
            percent_out = int(round(smooth))
            state.last_percent = smooth
            state.last_update_ts = now
    else:
        percent_out = int(round(smooth))
        state.last_percent = smooth
        state.last_update_ts = now

    min_eff = state.min_effective_percent
    if (
        min_eff is not None
        and min_eff > 0.0
        and percent_out > 0
        and percent_out < min_eff
    ):
        percent_out = int(round(min_eff))
        state.last_percent = float(percent_out)
        state.last_update_ts = now
        _LOGGER.debug(
            "better_thermostat %s: MPC clamp percent_out (%s) to min_effective=%s",
            name,
            entity,
            _round_for_debug(min_eff, 2),
        )

    temp_delta: Optional[float] = None
    time_delta: Optional[float] = None

    if inp.trv_temp_C is None:
        state.last_trv_temp = None
        state.last_trv_temp_ts = 0.0
        state.dead_zone_hits = 0
    else:
        if state.last_trv_temp is None or state.last_trv_temp_ts == 0.0:
            state.last_trv_temp = inp.trv_temp_C
            state.last_trv_temp_ts = now
        else:
            temp_delta = inp.trv_temp_C - state.last_trv_temp
            time_delta = now - state.last_trv_temp_ts
            eval_after = max(params.deadzone_time_s, 1.0)
            if time_delta >= eval_after:
                tol = max(inp.tolerance_K, 0.0)
                needs_heat = delta_t is not None and delta_t > tol
                small_command = 0 < percent_out <= params.deadzone_threshold_pct
                weak_response = (
                    temp_delta is None or temp_delta <= params.deadzone_temp_delta_K
                )

                if small_command and needs_heat and weak_response:
                    state.dead_zone_hits += 1
                    _LOGGER.debug(
                        "better_thermostat %s: MPC dead-zone observation (%s) hits=%s/%s temp_delta=%s command=%s%%",
                        name,
                        entity,
                        state.dead_zone_hits,
                        params.deadzone_hits_required,
                        _round_for_debug(temp_delta, 3),
                        percent_out,
                    )
                    if (
                        params.deadzone_hits_required > 0
                        and state.dead_zone_hits >= params.deadzone_hits_required
                    ):
                        proposed = percent_out + params.deadzone_raise_pct
                        current_min = state.min_effective_percent or 0.0
                        state.min_effective_percent = min(
                            100.0, max(current_min, proposed)
                        )
                        state.dead_zone_hits = 0
                        _LOGGER.debug(
                            "better_thermostat %s: MPC dead-zone raise (%s) proposed=%s new_min=%s",
                            name,
                            entity,
                            _round_for_debug(proposed, 2),
                            _round_for_debug(state.min_effective_percent, 2),
                        )
                else:
                    prev_hits = state.dead_zone_hits
                    if (
                        state.min_effective_percent is not None
                        and temp_delta is not None
                        and temp_delta > params.deadzone_temp_delta_K
                    ):
                        new_min = (
                            state.min_effective_percent - params.deadzone_decay_pct
                        )
                        state.min_effective_percent = new_min if new_min > 0.0 else None
                        _LOGGER.debug(
                            "better_thermostat %s: MPC dead-zone decay (%s) temp_delta=%s new_min=%s",
                            name,
                            entity,
                            _round_for_debug(temp_delta, 3),
                            _round_for_debug(state.min_effective_percent, 2),
                        )
                    state.dead_zone_hits = 0
                    if prev_hits:
                        _LOGGER.debug(
                            "better_thermostat %s: MPC dead-zone reset (%s) prev_hits=%s temp_delta=%s",
                            name,
                            entity,
                            prev_hits,
                            _round_for_debug(temp_delta, 3),
                        )

                state.last_trv_temp = inp.trv_temp_C
                state.last_trv_temp_ts = now

    min_eff = state.min_effective_percent
    if (
        min_eff is not None
        and min_eff > 0.0
        and percent_out > 0
        and percent_out < min_eff
    ):
        percent_out = int(round(min_eff))
        state.last_percent = float(percent_out)
        state.last_update_ts = now
        _LOGGER.debug(
            "better_thermostat %s: MPC clamp percent_out (%s) to min_effective=%s",
            name,
            entity,
            _round_for_debug(min_eff, 2),
        )

    debug: Dict[str, Any] = {
        "raw_percent": _round_for_debug(raw_percent, 2),
        "smooth_percent": _round_for_debug(smooth, 2),
        "too_soon": too_soon,
        "target_changed": target_changed,
        "delta_T": _round_for_debug(delta_t, 3),
        "min_effective_percent": (
            _round_for_debug(state.min_effective_percent, 2)
            if state.min_effective_percent is not None
            else None
        ),
        "dead_zone_hits": state.dead_zone_hits,
        "trv_temp_delta": _round_for_debug(temp_delta, 3),
        "trv_time_delta_s": _round_for_debug(time_delta, 1),
    }

    if inp.temp_slope_K_per_min is not None:
        if state.ema_slope is None:
            state.ema_slope = inp.temp_slope_K_per_min
        else:
            state.ema_slope = 0.6 * state.ema_slope + 0.4 * inp.temp_slope_K_per_min
        debug["slope_ema"] = _round_for_debug(state.ema_slope, 4)

    return percent_out, debug, delta_t
