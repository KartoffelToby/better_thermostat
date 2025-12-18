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
    mpc_thermal_gain: float = 0.06
    mpc_loss_coeff: float = 0.01
    mpc_control_penalty: float = 0.00005
    mpc_change_penalty: float = 0.2
    mpc_adapt: bool = True
    mpc_gain_min: float = 0.01
    mpc_gain_max: float = 0.2
    mpc_loss_min: float = 0.002
    mpc_loss_max: float = 0.03
    mpc_adapt_alpha: float = 0.01
    deadzone_threshold_pct: float = 20.0
    deadzone_temp_delta_K: float = 0.1
    deadzone_time_s: float = 300.0
    deadzone_hits_required: int = 3
    deadzone_raise_pct: float = 2.0
    deadzone_decay_pct: float = 1.0
    mpc_du_max_pct: float = 25.0
    min_percent_hold_time_s: float = 300.0  # mind. 5 Minuten Haltezeit
    big_change_force_open_pct: float = 33.0  # >33% Änderung darf sofort fahren


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
    last_learn_time: Optional[float] = None
    last_learn_temp: Optional[float] = None
    virtual_temp: Optional[float] = None
    virtual_temp_ts: float = 0.0
    trv_profile: str = "unknown"
    profile_confidence: float = 0.0
    profile_samples: int = 0


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
    "last_learn_time",
    "last_learn_temp",
    "virtual_temp",
    "virtual_temp_ts",
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
        state.last_learn_time = None
        state.last_learn_temp = None
        state.virtual_temp = None
        state.virtual_temp_ts = 0.0
        state.last_percent = None
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
            # --------------------------------------------
            # VIRTUAL TEMPERATURE FORWARD PREDICTION
            # --------------------------------------------
            if state.virtual_temp is not None and state.last_percent is not None:
                time_since_virtual = now - state.virtual_temp_ts

                if time_since_virtual > 0.5:
                    dt_min = time_since_virtual / 60.0

                    u = max(0.0, min(100.0, state.last_percent)) / 100.0
                    gain_est = (
                        float(state.gain_est)
                        if state.gain_est is not None
                        else float(params.mpc_thermal_gain)
                    )
                    loss_est = (
                        float(state.loss_est)
                        if state.loss_est is not None
                        else float(params.mpc_loss_coeff)
                    )

                    gain = max(params.mpc_gain_min, min(params.mpc_gain_max, gain_est))
                    loss = max(params.mpc_loss_min, min(params.mpc_loss_max, loss_est))

                    predicted_dT = gain * u * dt_min - loss * dt_min

                    state.virtual_temp += predicted_dT
                    state.virtual_temp_ts = now

                    _LOGGER.debug(
                        "better_thermostat %s: MPC virtual-temp forward %.4fK (u=%.1f, gain=%.4f, loss=%.4f)",
                        inp.bt_name or "BT",
                        predicted_dT,
                        u * 100,
                        gain,
                        loss,
                    )

            # --------------------------------------------
            # DELTA T USING VIRTUAL TEMPERATURE
            # --------------------------------------------
            if state.virtual_temp is not None and inp.target_temp_C is not None:
                delta_t = inp.target_temp_C - state.virtual_temp
            elif inp.target_temp_C is not None and inp.current_temp_C is not None:
                delta_t = inp.target_temp_C - inp.current_temp_C
            initial_delta_t = delta_t
            percent, mpc_debug = _compute_predictive_percent(
                inp, params, state, now, float(delta_t) if delta_t is not None else 0.0
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

    debug.update({"percent_out": percent_out})

    summary_delta = delta_t if delta_t is not None else initial_delta_t
    min_eff = state.min_effective_percent
    summary_gain = extra_debug.get("mpc_gain")
    summary_loss = extra_debug.get("mpc_loss")
    summary_horizon = extra_debug.get("mpc_horizon")
    summary_eval = extra_debug.get("mpc_eval_count")
    summary_cost = extra_debug.get("mpc_cost")

    _LOGGER.debug(
        "better_thermostat %s: mpc calibration for %s: e0=%sK gain=%s loss=%s horizon=%s | raw=%s%% out=%s%% min_eff=%s%% last=%s%% dead_hits=%s eval=%s cost=%s",
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
    )

    return MpcOutput(valve_percent=percent_out, debug=debug)


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

    assert inp.current_temp_C is not None
    assert inp.target_temp_C is not None

    current_temp_C = float(inp.current_temp_C)
    target_temp_C = float(inp.target_temp_C)

    # delta_t is kept for API/backward compatibility (pre-u0 versions used it)
    _ = delta_t

    if state.last_learn_time is None:
        state.last_learn_time = now
        state.last_learn_temp = inp.current_temp_C

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
    dt_last = now - state.last_learn_time

    # ---- ADAPTATION (rate-based identification) ----
    # Model: dT/dt ~= gain * u - loss, where gain/loss are in °C/min and u in [0..1]
    adapt_debug: Dict[str, Any] = {}
    if params.mpc_adapt and state.last_learn_temp is not None and dt_last >= 180.0:
        try:
            # Gate on target stability to avoid learning during setpoint steps.
            target_changed = False
            if state.last_target_C is not None:
                target_changed = (
                    abs(float(target_temp_C) - float(state.last_target_C)) >= 0.05
                )

            last_percent = state.last_percent if state.last_percent is not None else 0.0
            u_last = max(0.0, min(100.0, float(last_percent))) / 100.0

            dt_min = dt_last / 60.0
            if dt_min <= 0:
                dt_min = 0.0

            # measured temperature change
            delta_T = float(inp.current_temp_C) - float(state.last_learn_temp)
            observed_rate = (delta_T / dt_min) if dt_min > 0 else 0.0  # °C/min

            # Learn only when the sensor actually changed (quantised sensors).
            temp_change_threshold_C = float(
                getattr(params, "mpc_temp_change_threshold_C", 0.05)
            )
            if temp_change_threshold_C <= 0:
                temp_change_threshold_C = 0.05
            temp_changed = abs(delta_T) >= temp_change_threshold_C

            # sanity: avoid learning on extreme transients / sensor jumps
            # (typical indoor rate is far below 1°C/min)
            max_abs_rate = float(getattr(params, "mpc_max_abs_rate_C_per_min", 0.35))
            if max_abs_rate <= 0:
                max_abs_rate = 0.35
            rate_ok = abs(observed_rate) <= max_abs_rate

            min_open = (state.min_effective_percent or 5.0) / 100.0

            gain_est = (
                float(state.gain_est)
                if state.gain_est is not None
                else float(params.mpc_thermal_gain)
            )
            loss_est = (
                float(state.loss_est)
                if state.loss_est is not None
                else float(params.mpc_loss_coeff)
            )

            updated_gain = False
            updated_loss = False
            loss_method: Optional[str] = None

            # Common gates: don't learn during setpoint steps or crazy sensor jumps.
            common_ok = (not target_changed) and rate_ok and dt_min > 0

            # --- LOSS learning: u ~= 0 and room cooling (or not warming) ---
            # Needs a real temperature change (quantised sensors).
            if (
                common_ok
                and temp_changed
                and u_last <= min_open
                and observed_rate < -0.01
            ):
                loss_candidate = max(0.0, -observed_rate)
                loss_candidate = min(loss_candidate, params.mpc_loss_max)

                alpha = params.mpc_adapt_alpha
                if loss_candidate > loss_est:
                    alpha = params.mpc_adapt_alpha * 0.3  # slower increase
                state.loss_est = (1.0 - alpha) * loss_est + alpha * loss_candidate
                updated_loss = True
                loss_method = "cool_u0"

            # --- LOSS learning (residual): works even when valves never close ---
            # Important: this must work even when delta_T == 0 (steady-state), so it
            # must NOT depend on temp_changed. Instead, gate on quasi steady-state.
            residual_ok = False
            residual_rate_limited = False
            residual_block_jump = False
            u0_frac_dbg = (loss_est / gain_est) if gain_est > 0 else 0.0
            if common_ok and (not updated_loss) and u_last > min_open:
                ss_rate_thr = 0.02  # °C/min: quasi steady-state threshold
                u0_band = 0.10  # fraction: |u_last - u0| must be within this band

                # Rate-limit residual learning using dt_last window.
                # MPC is typically called every 5min; we don't want to learn on every call.
                # Also avoid learning when we currently observe a quantized temperature step.
                residual_min_interval_s = 600.0  # 10min
                residual_max_interval_s = 1800.0  # 30min (avoid stale windows)
                if (
                    dt_last < residual_min_interval_s
                    or dt_last > residual_max_interval_s
                ):
                    residual_rate_limited = True
                if temp_changed:
                    residual_block_jump = True

                u0_frac = (loss_est / gain_est) if gain_est > 0 else 0.0
                u0_frac = max(0.0, min(1.0, u0_frac))
                u0_near = abs(u_last - u0_frac) <= u0_band
                residual_ok = (
                    (not residual_rate_limited)
                    and (not residual_block_jump)
                    and u0_near
                    and abs(observed_rate) <= ss_rate_thr
                )

                if residual_ok:
                    # Use current gain estimate; avoid division.
                    loss_candidate = (gain_est * u_last) - observed_rate
                    loss_candidate = min(
                        max(loss_candidate, params.mpc_loss_min), params.mpc_loss_max
                    )

                    alpha = params.mpc_adapt_alpha
                    if loss_candidate > loss_est:
                        alpha = params.mpc_adapt_alpha * 0.3  # slower increase
                    state.loss_est = (1.0 - alpha) * loss_est + alpha * loss_candidate
                    updated_loss = True
                    loss_method = "residual_u0_ss"

            # --- GAIN learning: u > min_open and room warming ---
            # Needs a real temperature change (quantised sensors).
            if (
                common_ok
                and temp_changed
                and u_last > min_open
                and observed_rate > 0.01
            ):
                # gain = (observed_rate + loss) / u
                denom = max(u_last, 1e-3)
                gain_candidate = (observed_rate + loss_est) / denom

                gain_candidate = min(
                    max(gain_candidate, params.mpc_gain_min), params.mpc_gain_max
                )

                alpha = params.mpc_adapt_alpha
                if gain_candidate > gain_est:
                    alpha = params.mpc_adapt_alpha * 0.3  # slower increase
                state.gain_est = (1.0 - alpha) * gain_est + alpha * gain_candidate
                updated_gain = True

            # clamp to allowed physical range
            if state.gain_est is not None:
                state.gain_est = max(
                    params.mpc_gain_min, min(params.mpc_gain_max, float(state.gain_est))
                )
            if state.loss_est is not None:
                state.loss_est = max(
                    params.mpc_loss_min, min(params.mpc_loss_max, float(state.loss_est))
                )

            adapt_debug = {
                "id_dt_min": _round_for_debug(dt_min, 3),
                "id_delta_T": _round_for_debug(delta_T, 3),
                "id_temp_changed": temp_changed,
                "id_temp_change_threshold_C": _round_for_debug(
                    temp_change_threshold_C, 3
                ),
                "id_rate": _round_for_debug(observed_rate, 4),
                "id_rate_ok": rate_ok,
                "id_u_last": _round_for_debug(u_last, 3),
                "id_target_changed": target_changed,
                "id_gain_updated": updated_gain,
                "id_loss_updated": updated_loss,
                "id_loss_method": loss_method,
                "id_u0": _round_for_debug(u0_frac_dbg, 3),
                "id_u0_band": _round_for_debug(0.10, 3),
                "id_loss_ss_rate_thr": _round_for_debug(0.02, 4),
                "id_residual_ok": residual_ok,
                "id_residual_rate_limited": residual_rate_limited,
                "id_residual_block_jump": residual_block_jump,
            }

            state.last_learn_time = now
            state.last_learn_temp = inp.current_temp_C

        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # convert to per-step quantities (°C per simulation step)
    gain = state.gain_est if state.gain_est is not None else params.mpc_thermal_gain
    loss = state.loss_est if state.loss_est is not None else params.mpc_loss_coeff
    gain_step = gain * step_minutes
    loss_step = loss * step_minutes

    # ------------------------------------------------------------
    # BASE LOAD u0
    # u0 represents the steady-state opening where gain * u0 == loss.
    # The optimizer must not control absolute u anymore; it controls du around u0.
    # u_abs = u0 + du is applied AFTER solving (before clamping downstream).
    # ------------------------------------------------------------
    u0_frac: float
    if gain and gain > 0:
        u0_frac = loss / gain
    else:
        u0_frac = 0.0
    u0_frac = max(0.0, min(1.0, u0_frac))
    # Only clamp baseline by learned min_effective_percent once we actually have evidence.
    if state.min_effective_percent is not None and state.min_effective_percent > 0.0:
        min_eff = float(state.min_effective_percent) / 100.0
        u0_frac = max(u0_frac, min_eff)

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
    eval_count = 0

    def simulate_cost_for_candidate(u_abs_frac: float) -> float:
        """Simulate forward temperature for constant u_abs_frac (0..1) over horizon and return cost."""
        T = (
            float(state.virtual_temp)
            if state.virtual_temp is not None
            else current_temp_C
        )

        cost = 0.0
        for _ in range(horizon):
            # Work in du around u0 to keep the model consistent with the u0 formulation.
            # Base model: dT = gain_step * (u_abs - u0)
            du = u_abs_frac - u0_frac
            effective_u = max(0.0, du)

            effective_heating = gain_step * effective_u
            T_raw = T + effective_heating - loss_step
            T = T + lag_alpha * (T_raw - T)
            e = target_temp_C - T
            cost += e * e
        return cost

    # coarse search over du around u0
    # du_pct is additive on a 0..100% scale and can be negative.
    coarse_candidates = list(range(-100, 101, 1))
    best_du_coarse = 0
    best_cost_coarse = None
    for cand in coarse_candidates:
        du_frac = cand / 100.0
        u_abs_frac = u0_frac + du_frac
        # solver operates on du; clamp only for simulation validity
        u_abs_frac = max(0.0, min(1.0, u_abs_frac))
        cost = simulate_cost_for_candidate(u_abs_frac)
        eval_count += horizon
        # penalties
        cost += control_pen * (du_frac * du_frac)
        if last_percent is not None:
            # change penalty should apply to absolute command change
            last_du = (last_percent / 100.0) - u0_frac
            cost += change_pen * abs(du_frac - last_du)
            # cost += change_pen * abs(u_abs_frac - (last_percent / 100.0))
        if best_cost_coarse is None or cost < best_cost_coarse:
            best_cost_coarse = cost
            best_du_coarse = cand

    # fine search around best coarse du ±10% in 2% steps
    best_du_fine = best_du_coarse if best_du_coarse is not None else 0
    best_cost_fine = best_cost_coarse if best_cost_coarse is not None else float("inf")
    fine_lo = max(-100, best_du_coarse - 10)
    fine_hi = min(100, best_du_coarse + 10)
    for cand in range(fine_lo, fine_hi + 1, 2):
        du_frac = cand / 100.0
        u_abs_frac = u0_frac + du_frac
        u_abs_frac = max(0.0, min(1.0, u_abs_frac))
        cost = simulate_cost_for_candidate(u_abs_frac)
        eval_count += horizon
        cost += control_pen * (du_frac * du_frac)
        if last_percent is not None:
            last_du = (last_percent / 100.0) - u0_frac
            cost += change_pen * abs(du_frac - last_du)
            # cost += change_pen * abs(u_abs_frac - (last_percent / 100.0))
        if cost < best_cost_fine:
            best_cost_fine = cost
            best_du_fine = cand

    # result before postprocessing: convert du back to absolute percent around u0
    du_percent = float(best_du_fine)
    u_abs_percent = (u0_frac * 100.0) + du_percent
    best_percent = u_abs_percent

    # store last estimates
    state.last_temp = (
        state.virtual_temp if state.virtual_temp is not None else inp.current_temp_C
    )
    state.last_time = now

    # build debug
    mpc_debug = {
        "mpc_gain": _round_for_debug(gain, 4),
        "mpc_loss": _round_for_debug(loss, 4),
        "mpc_u0_pct": _round_for_debug(u0_frac * 100.0, 3),
        "mpc_du_pct": _round_for_debug(du_percent, 3),
        "mpc_u_abs_pct": _round_for_debug(u_abs_percent, 3),
        "mpc_horizon": horizon,
        "mpc_eval_count": eval_count,
        "mpc_step_minutes": _round_for_debug(step_minutes, 3),
    }

    if adapt_debug:
        mpc_debug.update(adapt_debug)

    if best_cost_fine is not None:
        mpc_debug["mpc_cost"] = _round_for_debug(best_cost_fine, 6)

    if last_percent is not None:
        mpc_debug["mpc_last_percent"] = _round_for_debug(last_percent, 2)

    return best_percent, mpc_debug


def _detect_trv_profile(
    state: _MpcState,
    percent_out: float,
    temp_delta: Optional[float],
    time_delta: Optional[float],
    expected_temp_rise: float,
    params: MpcParams,
) -> None:

    if temp_delta is None or time_delta is None or time_delta <= 0:
        return

    if percent_out <= 0 or expected_temp_rise <= 0:
        return

    if expected_temp_rise <= 0:
        return

    response_ratio = temp_delta / expected_temp_rise
    state.profile_samples += 1

    is_small = percent_out <= params.deadzone_threshold_pct
    weak_response = response_ratio < 0.3

    threshold_evidence = 1.0 if (is_small and weak_response) else 0.0
    linear_evidence = max(0.0, 1.0 - abs(response_ratio - 1.0))
    exponential_evidence = 1.0 if (percent_out > 50 and response_ratio > 1.2) else 0.0

    alpha = 0.1

    if threshold_evidence > 0.5:
        state.trv_profile = "threshold"
        state.profile_confidence = min(1.0, state.profile_confidence + alpha)

    elif linear_evidence > 0.5:
        state.trv_profile = "linear"
        state.profile_confidence = min(
            1.0, state.profile_confidence + alpha * linear_evidence
        )

    elif exponential_evidence > 0.5:
        state.trv_profile = "exponential"
        state.profile_confidence = min(
            1.0, state.profile_confidence + alpha * exponential_evidence
        )

    if state.profile_samples >= 20 and state.profile_confidence > 0.7:
        _apply_profile_adjustments(state, params)


def _apply_profile_adjustments(state: _MpcState, params: MpcParams) -> None:

    if state.trv_profile == "threshold":
        # Do not directly force a permanent min opening here.
        # A threshold-like TRV should be handled by dead-zone learning (raise/decay)
        # so it can adapt and revert if conditions change.
        return

    elif state.trv_profile == "exponential":
        if state.gain_est is None:
            state.gain_est = params.mpc_thermal_gain
        state.gain_est *= 1.1
        state.gain_est = min(params.mpc_gain_max, state.gain_est)

    elif state.trv_profile == "linear":
        # Don't wipe learned min opening unconditionally; let dead-zone decay handle it.
        return


def _post_process_percent(
    inp: MpcInput,
    params: MpcParams,
    state: _MpcState,
    now: float,
    raw_percent: float,
    delta_t: Optional[float],
) -> tuple[int, Dict[str, Any], Optional[float]]:
    """Apply smoothing, hysteresis, min-effective, du_max, dead-zone detection and produce debug info."""

    name = inp.bt_name or "BT"
    entity = inp.entity_id or "unknown"

    # --------------------------------------------
    # VIRTUAL TEMPERATURE SYNCHRONISATION
    # --------------------------------------------
    if inp.current_temp_C is not None:
        if state.virtual_temp is None:
            state.virtual_temp = inp.current_temp_C
        else:
            alpha = 0.3  # Sensorvertrauen
            state.virtual_temp = (
                alpha * inp.current_temp_C + (1 - alpha) * state.virtual_temp
            )
        state.virtual_temp_ts = now

    # ============================================================
    # 1) INITIAL RAW VALUE
    # ============================================================
    smooth = raw_percent
    target_changed = False

    # track target temp changes
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

    # update frequency limiter
    too_soon = (now - state.last_update_ts) < params.min_update_interval_s
    if target_changed:
        too_soon = False

    # compute delta_t if missing
    if inp.target_temp_C is not None and inp.current_temp_C is not None:
        try:
            if delta_t is None:
                delta_t = inp.target_temp_C - inp.current_temp_C
        except (TypeError, ValueError):
            delta_t = None

    # ============================================================
    # 2) MIN EFFECTIVE OPENING (FIRST!)
    # ============================================================
    min_eff = state.min_effective_percent
    if min_eff is not None and min_eff > 0.0 and smooth > 0.0 and smooth < min_eff:
        _LOGGER.debug(
            "better_thermostat %s: MPC pre-smooth clamp (%s) to min_effective=%s",
            name,
            entity,
            _round_for_debug(min_eff, 2),
        )
        smooth = min_eff

    # ============================================================
    # 3) DU_MAX LIMIT (MAX STEPPING)
    # ============================================================
    last_percent = state.last_percent
    du_max = getattr(params, "mpc_du_max_pct", None)

    if last_percent is not None and du_max is not None and du_max > 0:
        delta = smooth - last_percent
        if abs(delta) > du_max:
            limited = last_percent + du_max * (1 if delta > 0 else -1)
            _LOGGER.debug(
                "better_thermostat %s: MPC du_max-limit (%s) raw=%s → limited=%s (max %s)",
                name,
                entity,
                _round_for_debug(smooth, 2),
                _round_for_debug(limited, 2),
                du_max,
            )
            smooth = limited

    # ============================================================
    # 4) HYSTERESIS
    # ============================================================
    if last_percent is not None:
        change = abs(smooth - last_percent)
        if (change < params.percent_hysteresis_pts and not target_changed) or too_soon:
            percent_out = int(round(last_percent))
        else:
            percent_out = int(round(smooth))
    else:
        percent_out = int(round(smooth))

    # ============================================================
    # 5) FINAL MIN EFFECTIVE CHECK ON INTEGER OUTPUT
    # ============================================================
    min_eff = state.min_effective_percent
    if (
        min_eff is not None
        and min_eff > 0.0
        and percent_out > 0
        and percent_out < min_eff
    ):
        _LOGGER.debug(
            "better_thermostat %s: MPC final clamp percent_out (%s) to min_effective=%s",
            name,
            _round_for_debug(percent_out, 2),
            _round_for_debug(min_eff, 2),
        )
        percent_out = int(round(min_eff))

    # ============================================================
    # 6) DEAD-ZONE DETECTION (improved)
    # ============================================================
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

            if time_delta >= eval_after and state.trv_profile == "unknown":

                tol = max(inp.tolerance_K, 0.0)
                needs_heat = delta_t is not None and delta_t > tol
                small_command = 0 < percent_out <= params.deadzone_threshold_pct
                weak_response = (
                    temp_delta is None or temp_delta <= params.deadzone_temp_delta_K
                )

                # --- Expected MPC effect vs real heating ---
                gain = state.gain_est or params.mpc_thermal_gain
                expected_temp_rise = gain * (percent_out / 100.0) * (time_delta / 60.0)

                # --- TRV Profile Detection ---
                _detect_trv_profile(
                    state,
                    percent_out,
                    temp_delta,
                    time_delta,
                    expected_temp_rise,
                    params,
                )

                # Optional: upstream may attach a previous room temp dynamically.
                last_room_temp_C = getattr(inp, "last_room_temp_C", None)
                room_temp_delta = (
                    (inp.current_temp_C - last_room_temp_C)
                    if last_room_temp_C is not None and inp.current_temp_C is not None
                    else None
                )

                measured_ok = (
                    room_temp_delta is not None
                    and room_temp_delta > expected_temp_rise * 0.2
                )

                trv_self_heating = (
                    temp_delta is not None
                    and temp_delta > 0.0
                    and room_temp_delta is not None
                    and room_temp_delta <= 0.0
                )

                # --- DEADZONE HIT (improved logic) ---
                deadzone_hit = (
                    small_command
                    and needs_heat
                    and (weak_response or trv_self_heating or not measured_ok)
                )

                if deadzone_hit:
                    state.dead_zone_hits += 1
                    _LOGGER.debug(
                        "better_thermostat %s: MPC dead-zone HIT (%s) hits=%s/%s "
                        "trv_temp_delta=%s room_temp_delta=%s expected=%s cmd=%s%%",
                        name,
                        entity,
                        state.dead_zone_hits,
                        params.deadzone_hits_required,
                        _round_for_debug(temp_delta, 3),
                        (
                            _round_for_debug(room_temp_delta, 3)
                            if room_temp_delta is not None
                            else None
                        ),
                        _round_for_debug(expected_temp_rise, 3),
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
                            "better_thermostat %s: MPC dead-zone RAISE (%s) new_min=%s",
                            name,
                            entity,
                            _round_for_debug(state.min_effective_percent, 2),
                        )

                else:
                    # --- Reset / decay ---
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
                            "better_thermostat %s: MPC dead-zone DECAY (%s) new_min=%s",
                            name,
                            entity,
                            _round_for_debug(state.min_effective_percent, 2),
                        )

                    state.dead_zone_hits = 0
                    if prev_hits:
                        _LOGGER.debug(
                            "better_thermostat %s: MPC dead-zone RESET (%s) prev_hits=%s",
                            name,
                            entity,
                            prev_hits,
                        )

            else:
                # deadzone fully disabled because TRV profile is known
                pass

            state.last_trv_temp = inp.trv_temp_C
            state.last_trv_temp_ts = now
    # 7) DEBUG INFO
    # ============================================================
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
        "trv_profile": state.trv_profile,
        "trv_profile_conf": _round_for_debug(state.profile_confidence, 3),
        "trv_profile_samples": state.profile_samples,
        "trv_temp_delta": _round_for_debug(temp_delta, 3),
        "trv_time_delta_s": _round_for_debug(time_delta, 1),
    }

    # slope EMA unchanged
    if inp.temp_slope_K_per_min is not None:
        if state.ema_slope is None:
            state.ema_slope = inp.temp_slope_K_per_min
        else:
            state.ema_slope = 0.6 * state.ema_slope + 0.4 * inp.temp_slope_K_per_min
        debug["slope_ema"] = _round_for_debug(state.ema_slope, 4)

    # ===========================================
    # MINIMUM HOLD TIME – ANTI-CHATTERING
    # ===========================================
    hold_time = params.min_percent_hold_time_s

    # Wenn wir kurz vorher ein anderes Kommando geschickt haben → blockieren
    if state.last_percent is not None:
        time_since_update = now - state.last_update_ts

        # Ausnahme: Target wurde geändert → immer erlauben
        if not target_changed:

            # Ausnahme: große Änderung nötig (z.B. Fenster auf)
            big_change = (
                abs(smooth - state.last_percent) >= params.big_change_force_open_pct
            )

            if time_since_update < hold_time and not big_change:
                percent_out = int(round(state.last_percent))
                debug["hold_block"] = True
                debug["hold_remaining_s"] = int(hold_time - time_since_update)
                _LOGGER.debug(
                    "better_thermostat %s: MPC hold-time block (%s) last_percent=%s output=%s remaining_s=%s",
                    name,
                    entity,
                    _round_for_debug(state.last_percent, 2),
                    percent_out,
                    _round_for_debug(hold_time - time_since_update, 1),
                )

    # ============================================================
    # 8) UPDATE STATE ONLY IF CHANGED
    # ============================================================
    # Only update last_percent and last_update_ts if the output actually changed
    original_last_percent = state.last_percent
    if original_last_percent is None or abs(percent_out - original_last_percent) >= 0.5:
        state.last_percent = float(percent_out)
        state.last_update_ts = now

    return percent_out, debug, delta_t
