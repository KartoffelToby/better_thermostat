"""Lightweight MPC helper independent from balance logic."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import math
import random
from time import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


# MPC operates on fixed 5-minute steps and a 6-step horizon.
MPC_STEP_SECONDS = 300.0
MPC_HORIZON_STEPS = 6


@dataclass
class MpcParams:
    """Configuration for the predictive controller."""

    cap_max_K: float = 0.8
    percent_hysteresis_pts: float = 0.5
    min_update_interval_s: float = 60.0
    mpc_thermal_gain: float = 0.06
    mpc_loss_coeff: float = 0.01
    mpc_control_penalty: float = 0.05
    mpc_change_penalty: float = 1.0
    mpc_eco_penalty: float = 1.0
    mpc_adapt: bool = True
    mpc_gain_min: float = 0.01
    mpc_gain_max: float = 0.2
    mpc_loss_min: float = 0.002
    mpc_loss_max: float = 0.03
    mpc_solar_gain_initial: float = (
        0.01  # Initial guess for solar gain (°C/min at full sun)
    )
    mpc_solar_gain_max: float = 0.05
    mpc_adapt_alpha: float = 0.1
    mpc_adapt_window_block_s: float = 900.0
    deadzone_threshold_pct: float = 20.0
    deadzone_temp_delta_K: float = 0.1
    deadzone_time_s: float = 300.0
    deadzone_hits_required: int = 3
    deadzone_raise_pct: float = 2.0
    deadzone_decay_pct: float = 1.0
    mpc_du_max_pct: float = 25.0
    min_percent_hold_time_s: float = 300.0  # mind. 5 Minuten Haltezeit
    big_change_force_open_pct: float = 33.0  # >33% Änderung darf sofort fahren

    # Minimum effective opening / dead-zone learning.
    # If disabled, small commands are not clamped up and dead-zone raise/decay is skipped.
    enable_min_effective_percent: bool = False

    # Virtual temperature behaviour.
    # When enabled, `virtual_temp` is used as the MPC state temperature and can be
    # forward-predicted between sensor updates via a Kalman filter.
    use_virtual_temp: bool = True

    # Kalman filter tuning for virtual temperature.
    # Q = process noise (model uncertainty), R = measurement noise (sensor noise).
    # Higher Q/R ratio trusts the sensor more; lower trusts the model more.
    kalman_Q: float = 0.001  # Process noise variance (°C²) per second
    kalman_R: float = 0.04  # Measurement noise variance (°C²)

    # TRV performance curve sampling (data collection only)
    perf_curve_min_window_s: float = 300.0
    perf_curve_bin_pct: float = 2.0


@dataclass
class MpcInput:
    """Input parameters for MPC calibration calculation."""

    key: str
    target_temp_C: float | None
    current_temp_C: float | None
    filtered_temp_C: float | None = None
    trv_temp_C: float | None = None
    tolerance_K: float = 0.0
    temp_slope_K_per_min: float | None = None
    window_open: bool = False
    heating_allowed: bool = True
    bt_name: str | None = None
    entity_id: str | None = None
    outdoor_temp_C: float | None = None
    is_day: bool = True
    other_heat_power: float = 0.0
    solar_intensity: float = 0.0  # 0.0 to 1.0 (cloud coverage, etc.)
    max_opening_pct: float | None = None


@dataclass
class MpcOutput:
    """Output result from MPC calibration calculation."""

    valve_percent: int
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass
class _MpcState:
    last_percent: float | None = None
    last_update_ts: float = 0.0
    last_target_C: float | None = None
    ema_slope: float | None = None
    gain_est: float | None = None
    loss_est: float | None = None
    ka_est: float | None = None  # Insulation coefficient (loss per degree diff)
    solar_gain_est: float | None = None  # Learned solar gain factor
    last_temp: float | None = None
    last_time: float = 0.0
    last_trv_temp: float | None = None
    last_trv_temp_ts: float = 0.0
    last_window_open_ts: float = 0.0
    dead_zone_hits: int = 0
    min_effective_percent: float | None = None
    last_learn_time: float | None = None
    last_learn_temp: float | None = None
    last_residual_time: float | None = None
    virtual_temp: float | None = None
    virtual_temp_ts: float = 0.0
    last_sensor_temp_C: float | None = None
    last_room_temp_C: float | None = None
    last_room_temp_ts: float = 0.0
    perf_curve: dict[str, dict[str, float | int]] = field(default_factory=dict)
    trv_profile: str = "unknown"
    profile_confidence: float = 0.0
    profile_samples: int = 0
    u_integral: float = 0.0
    time_integral: float = 0.0
    last_integration_ts: float = 0.0
    created_ts: float = 0.0
    loss_learn_count: int = 0
    gain_learn_count: int = 0
    is_calibration_active: bool = False
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=20))
    regime_boost_active: bool = False
    consecutive_insufficient_heat: int = 0
    kalman_P: float = 1.0  # Kalman filter error covariance


_MPC_STATES: dict[str, _MpcState] = {}

_STATE_EXPORT_FIELDS = (
    "last_percent",
    "last_update_ts",
    "last_target_C",
    "ema_slope",
    "gain_est",
    "loss_est",
    "ka_est",
    "solar_gain_est",
    "last_temp",
    "last_time",
    "last_trv_temp",
    "last_trv_temp_ts",
    "last_window_open_ts",
    "min_effective_percent",
    "dead_zone_hits",
    "last_learn_time",
    "last_learn_temp",
    "last_residual_time",
    "virtual_temp",
    "virtual_temp_ts",
    "last_room_temp_C",
    "last_room_temp_ts",
    "perf_curve",
    "u_integral",
    "time_integral",
    "last_integration_ts",
    "trv_profile",
    "profile_confidence",
    "profile_samples",
    "created_ts",
    "loss_learn_count",
    "gain_learn_count",
    "is_calibration_active",
    "recent_errors",
    "regime_boost_active",
    "consecutive_insufficient_heat",
    "kalman_P",
)


def _serialize_state(state: _MpcState) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for attr in _STATE_EXPORT_FIELDS:
        value = getattr(state, attr, None)
        if value is None:
            continue
        # Convert deque to list for JSON serialization
        if isinstance(value, deque):
            value = list(value)
        payload[attr] = value
    return payload


def export_mpc_state_map(prefix: str | None = None) -> dict[str, dict[str, Any]]:
    """Return a serializable mapping of MPC states, optionally filtered by key prefix."""

    exported: dict[str, dict[str, Any]] = {}
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
            if attr == "perf_curve" and isinstance(value, Mapping):
                setattr(state, attr, dict(value))
                continue
            if attr == "recent_errors" and isinstance(value, (list, tuple)):
                setattr(state, attr, deque(value, maxlen=20))
                continue
            try:
                coerced: int | float | bool
                if attr in ("dead_zone_hits", "loss_learn_count", "gain_learn_count"):
                    coerced = int(value)
                elif attr == "is_calibration_active":
                    coerced = bool(value)
                else:
                    coerced = float(value)
            except (TypeError, ValueError):
                continue
            setattr(state, attr, coerced)


def _curve_bin_label(percent: float, bin_pct: float) -> str:
    bin_pct = max(1.0, float(bin_pct))
    p = max(0.0, min(100.0, float(percent)))
    lo = math.floor(p / bin_pct) * bin_pct
    hi = min(100.0, lo + bin_pct)
    if bin_pct.is_integer():
        return f"p{int(lo):02d}_{int(hi):02d}"
    return f"p{lo:.1f}_{hi:.1f}"


def _update_perf_curve(
    state: _MpcState,
    inp: MpcInput,
    params: MpcParams,
    now: float,
    extra_debug: dict[str, Any],
) -> None:
    if inp.current_temp_C is None:
        return
    if not inp.heating_allowed or inp.window_open or inp.target_temp_C is None:
        state.last_room_temp_C = float(inp.current_temp_C)
        state.last_room_temp_ts = now
        return

    if state.last_room_temp_ts <= 0.0 or state.last_room_temp_C is None:
        state.last_room_temp_C = float(inp.current_temp_C)
        state.last_room_temp_ts = now
        return

    dt_s = now - state.last_room_temp_ts
    min_window = float(getattr(params, "perf_curve_min_window_s", 300.0))
    if dt_s < min_window:
        return

    room_delta = float(inp.current_temp_C) - float(state.last_room_temp_C)
    dt_min = dt_s / 60.0
    if dt_min <= 0:
        return

    room_rate = room_delta / dt_min

    if state.time_integral > 0:
        u_avg_pct = state.u_integral / state.time_integral
    else:
        u_avg_pct = float(state.last_percent) if state.last_percent is not None else 0.0

    bin_pct = float(getattr(params, "perf_curve_bin_pct", 5.0))
    label = _curve_bin_label(u_avg_pct, bin_pct)

    trv_rate = None
    if inp.trv_temp_C is not None and state.last_trv_temp is not None:
        trv_delta = float(inp.trv_temp_C) - float(state.last_trv_temp)
        trv_rate = trv_delta / dt_min

    temp_error = float(inp.target_temp_C) - float(inp.current_temp_C)

    stats = state.perf_curve.setdefault(
        label,
        {
            "count": 0,
            "avg_room_rate": 0.0,
            "avg_trv_rate": 0.0,
            "avg_percent": 0.0,
            "avg_temp_error": 0.0,
        },
    )

    count = int(stats.get("count", 0)) + 1
    stats["count"] = count
    stats["avg_room_rate"] = (
        stats.get("avg_room_rate", 0.0)
        + (room_rate - stats.get("avg_room_rate", 0.0)) / count
    )
    if trv_rate is not None:
        stats["avg_trv_rate"] = (
            stats.get("avg_trv_rate", 0.0)
            + (trv_rate - stats.get("avg_trv_rate", 0.0)) / count
        )
    stats["avg_percent"] = (
        stats.get("avg_percent", 0.0)
        + (float(u_avg_pct) - stats.get("avg_percent", 0.0)) / count
    )
    stats["avg_temp_error"] = (
        stats.get("avg_temp_error", 0.0)
        + (temp_error - stats.get("avg_temp_error", 0.0)) / count
    )

    extra_debug["perf_curve_bin"] = label
    extra_debug["perf_room_rate"] = _round_for_debug(room_rate, 4)

    state.last_room_temp_C = float(inp.current_temp_C)
    state.last_room_temp_ts = now


def _split_mpc_key(key: str) -> tuple[str | None, str | None, str | None]:
    try:
        uid, entity, bucket = key.split(":", 2)
        return uid, entity, bucket
    except ValueError:
        return None, None, None


def _seed_state_from_siblings(key: str, state: _MpcState, params: MpcParams) -> None:
    if not bool(getattr(params, "enable_min_effective_percent", True)):
        return
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
    """Return a stable key for MPC state tracking.

    For a single-TRV BT instance this key is entity-specific.
    For multi-TRV setups the caller should prefer ``build_mpc_group_key``
    so that all TRVs share a single MPC model.
    """

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


def build_mpc_group_key(bt) -> str:
    """Return a BT-level (group) key for MPC state tracking.

    All TRVs under the same BT instance share this key so that a single
    MPC model learns the room dynamics from the external sensor.
    """

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
    return f"{uid}:group:{bucket}"


# Compensation factor: additional valve-% per Kelvin that a TRV is colder
# than the warmest TRV in the group.  8 %/K means a TRV that is 3 K colder
# than the warmest one gets 24 % extra opening.
DISTRIBUTE_COMPENSATION_PCT_PER_K: float = 8.0


def distribute_valve_percent(
    u_total_pct: float, trv_temps: dict[str, float | None]
) -> dict[str, float]:
    """Distribute a group MPC valve command across TRVs based on internal temps.

    The **warmest TRV** is used as the reference and receives exactly
    ``u_total_pct`` (the MPC output).  Every colder TRV gets a boost
    proportional to how much colder it is than the warmest::

        result[trv] = u_total_pct + (T_warmest - T_trv) * COMPENSATION_PCT_PER_K

    This guarantees:
    * At least one TRV always receives the exact MPC-computed value.
    * Colder TRVs open further to compensate for their disadvantaged position
      (e.g. near a drafty window or far from the heat source).
    * Warmer TRVs are never penalised below the MPC value — the compensation
      only *adds* to the baseline.

    If all TRVs have the same (or ``None``) temperature the function returns
    ``u_total_pct`` for every TRV (uniform distribution).

    Parameters
    ----------
    u_total_pct:
        The group-level MPC output in percent (0–100).  This is assigned
        to the warmest TRV unchanged.
    trv_temps:
        Mapping ``entity_id → internal TRV temperature (°C)`` or ``None``
        if the temperature is unavailable.

    Returns
    -------
    dict[str, float]
        Mapping ``entity_id → adjusted valve percent``.
    """

    n = len(trv_temps)
    if n == 0:
        return {}

    # -- Fast path: single TRV or zero command --
    if n == 1 or u_total_pct <= 0.0:
        return {eid: max(0.0, min(100.0, u_total_pct)) for eid in trv_temps}

    # -- Collect valid temperatures --
    valid_temps: dict[str, float] = {}
    for eid, trv_temp in trv_temps.items():
        if trv_temp is not None and isinstance(trv_temp, (int, float)):
            valid_temps[eid] = float(trv_temp)

    if not valid_temps:
        # No valid TRV temperatures → uniform
        return {eid: max(0.0, min(100.0, u_total_pct)) for eid in trv_temps}

    # -- Reference: warmest TRV --
    warmest_temp = max(valid_temps.values())

    compensation = float(DISTRIBUTE_COMPENSATION_PCT_PER_K)

    result: dict[str, float] = {}
    for eid in trv_temps:
        if eid in valid_temps:
            deficit_K = warmest_temp - valid_temps[eid]
            pct = u_total_pct + deficit_K * compensation
        else:
            # No temperature data → assign baseline (neutral)
            pct = u_total_pct
        result[eid] = max(0.0, min(100.0, pct))

    return result


def _detect_regime_change(recent_errors: deque | list) -> bool:
    """Detect systematic bias in prediction errors using Student's t-test.

    If the mean error deviates significantly from 0 relative to standard deviation,
    it indicates a regime change (e.g. window opened, weather change).
    Adaptation should be boosted.
    """
    N = 10
    if len(recent_errors) < N:
        return False

    # Check last N errors
    errors_to_check = list(recent_errors)[-N:]
    mean_error = sum(errors_to_check) / N

    # Std deviation
    try:
        variance = sum((e - mean_error) ** 2 for e in errors_to_check) / N
        std_error = variance**0.5
    except Exception:
        return False

    if std_error == 0:
        # All errors are identical.  If mean is nonzero, the bias is
        # perfectly consistent and should be treated as a regime change.
        return mean_error != 0

    # t-statistic: how many std-devs is the mean away from 0?
    t_stat = abs(mean_error) / (std_error / (N**0.5))

    # Threshold > 2.0 is significant (approx 95% confidence)
    return t_stat > 2.0


def _round_for_debug(value: Any, digits: int = 3) -> Any:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return value


def compute_mpc(inp: MpcInput, params: MpcParams) -> MpcOutput | None:
    """Run the predictive controller and emit a valve recommendation."""

    now = time()
    state = _MPC_STATES.setdefault(inp.key, _MpcState())
    if state.created_ts == 0.0:
        # For existing trained models, backdate the creation timestamp
        # to avoid "Training" status if we already have confidence.
        if state.profile_confidence > 0.5:
            state.created_ts = time() - 90000.0
        else:
            state.created_ts = time()

    _seed_state_from_siblings(inp.key, state, params)

    if inp.window_open:
        state.last_window_open_ts = now

    # --- INTEGRATE VALVE USAGE ---
    # We track the time-weighted average of the valve position since the last learning step.
    # This ensures that if the valve moved during the learning interval (e.g. due to manual changes
    # or frequent updates), we learn from the average power applied, not just the last value.
    if state.last_integration_ts > 0.0:
        dt_int = now - state.last_integration_ts
        if dt_int > 0 and state.last_percent is not None:
            state.u_integral += float(state.last_percent) * dt_int
            state.time_integral += dt_int
    state.last_integration_ts = now

    extra_debug: dict[str, Any] = {}
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

    initial_delta_t: float | None = None

    if not inp.heating_allowed or inp.window_open:
        percent = 0.0
        delta_t = None
        state.last_learn_time = None
        state.last_learn_temp = None
        state.virtual_temp = None
        state.virtual_temp_ts = 0.0
        state.last_percent = None
        state.u_integral = 0.0
        state.time_integral = 0.0
        state.last_residual_time = None

        if state.is_calibration_active:
            state.is_calibration_active = False
            _LOGGER.info(
                "better_thermostat %s: Calibration aborted (window_open=%s, heating_allowed=%s).",
                name,
                inp.window_open,
                inp.heating_allowed,
            )

        _LOGGER.debug(
            "better_thermostat %s: MPC skip heating (%s) window_open=%s heating_allowed=%s",
            name,
            entity,
            inp.window_open,
            inp.heating_allowed,
        )
    elif inp.target_temp_C is None or inp.current_temp_C is None:
        percent = state.last_percent if state.last_percent is not None else 0.0
        delta_t = None
        _LOGGER.debug(
            "better_thermostat %s: MPC missing temps (%s) reusing last_percent=%s",
            name,
            entity,
            _round_for_debug(percent, 2),
        )
    else:
        use_virtual_temp = bool(getattr(params, "use_virtual_temp", True))

        # --------------------------------------------
        # KALMAN FILTER FOR VIRTUAL TEMPERATURE
        # --------------------------------------------
        # Replaces the previous 3-mechanism approach (forward prediction +
        # adaptive EMA sync + hard-reset/clamp) with a single, principled
        # Kalman filter.  Two phases per tick:
        #   1) PREDICT  – propagate state forward using the physics model
        #   2) UPDATE   – correct with a new sensor reading (if changed)
        #
        # State: x = virtual_temp (scalar)
        # Model: x_k+1 = x_k + (gain*u - loss)*dt_min
        # Measurement: z_k = sensor_temp (when it changes)
        if use_virtual_temp and inp.current_temp_C is not None:
            sensor_temp = float(inp.current_temp_C)
            Q = max(1e-6, float(params.kalman_Q))  # process noise / sec
            R = max(1e-6, float(params.kalman_R))  # measurement noise

            # --- PREDICT step ---
            if state.virtual_temp is None:
                # First call: initialise from sensor
                state.virtual_temp = sensor_temp
                state.virtual_temp_ts = now
                state.kalman_P = R  # initial uncertainty = measurement noise
                extra_debug["kalman_phase"] = "init"
            else:
                dt_s = max(0.0, now - state.virtual_temp_ts)
                if dt_s > 0.0 and state.last_percent is not None:
                    dt_min = dt_s / 60.0
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
                    gain_c = max(
                        params.mpc_gain_min, min(params.mpc_gain_max, gain_est)
                    )
                    loss_c = max(
                        params.mpc_loss_min, min(params.mpc_loss_max, loss_est)
                    )

                    predicted_dT = gain_c * u * dt_min - loss_c * dt_min
                    state.virtual_temp += predicted_dT
                    # P grows with process noise proportional to elapsed time
                    state.kalman_P += Q * dt_s

                    extra_debug["kalman_predict_dT"] = _round_for_debug(predicted_dT, 4)
                    extra_debug["kalman_P_predict"] = _round_for_debug(
                        state.kalman_P, 5
                    )

                state.virtual_temp_ts = now

            # --- UPDATE step (only when sensor actually changed) ---
            prev_sensor = state.last_sensor_temp_C
            sensor_changed = (
                prev_sensor is None or abs(sensor_temp - float(prev_sensor)) >= 0.001
            )

            if sensor_changed:
                P = state.kalman_P
                # Kalman gain: K = P / (P + R)
                K = P / (P + R)
                innovation = sensor_temp - float(state.virtual_temp)
                state.virtual_temp = float(state.virtual_temp) + K * innovation
                state.kalman_P = (1.0 - K) * P

                extra_debug["kalman_K"] = _round_for_debug(K, 4)
                extra_debug["kalman_innovation"] = _round_for_debug(innovation, 4)
                extra_debug["kalman_P_update"] = _round_for_debug(state.kalman_P, 5)
            else:
                extra_debug["kalman_update"] = "skipped_sensor_unchanged"

            state.last_sensor_temp_C = sensor_temp

        # --------------------------------------------
        # DELTA T USING VIRTUAL TEMPERATURE
        # --------------------------------------------
        if (
            use_virtual_temp
            and state.virtual_temp is not None
            and inp.target_temp_C is not None
        ):
            delta_t = inp.target_temp_C - state.virtual_temp
        elif inp.target_temp_C is not None and inp.current_temp_C is not None:
            delta_t = inp.target_temp_C - inp.current_temp_C
        initial_delta_t = delta_t
        percent, mpc_debug = _compute_predictive_percent(
            inp, params, state, now, float(delta_t) if delta_t is not None else 0.0
        )
        # Keep any virtual-temp debug collected earlier and merge MPC debug on top.
        extra_debug.update(mpc_debug)

        # --- FORCED LOSS CALIBRATION ---
        # "Hybrid Learning": Use random forced calibration to get high-confidence loss samples.
        # Trigger: current >= target. Probability: decays with number of samples.
        # Action: Force valve closed until current < target - hysteresis.
        if inp.current_temp_C is not None and inp.target_temp_C is not None:
            calib_hysteresis = 0.2
            if state.is_calibration_active:
                if inp.current_temp_C <= (inp.target_temp_C - calib_hysteresis):
                    state.is_calibration_active = False
                    _LOGGER.info(
                        "better_thermostat %s: Calibration finished (temp reached target - %.1fvK). Resuming control.",
                        name,
                        calib_hysteresis,
                    )
                else:
                    percent = 0.0
                    extra_debug["calib_active"] = True

            elif not state.is_calibration_active:
                # Trigger condition: Overheated/Reached target
                # Check random chance if we have "enough" heat (delta_t <= 0 means we are at/above target)
                if inp.current_temp_C >= inp.target_temp_C:
                    # Chance decays with experience: 1 (100%), 0.5, ... but min 5%
                    chance = max(0.05, 1.0 / (state.loss_learn_count + 1))
                    if random.random() < chance:
                        state.is_calibration_active = True
                        percent = 0.0
                        extra_debug["calib_active"] = True
                        _LOGGER.info(
                            "better_thermostat %s: Starting forced calibration (chance %.2f, count %d). Forcing 0%% valve.",
                            name,
                            chance,
                            state.loss_learn_count,
                        )

        _LOGGER.debug(
            "better_thermostat %s: MPC raw output (%s) percent=%s delta_T=%s debug=%s",
            name,
            entity,
            _round_for_debug(percent, 2),
            _round_for_debug(delta_t, 3),
            extra_debug,
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

    _update_perf_curve(state=state, inp=inp, params=params, now=now, extra_debug=debug)

    debug.update({"percent_out": percent_out})

    summary_delta = delta_t if delta_t is not None else initial_delta_t
    min_eff = state.min_effective_percent
    summary_gain = extra_debug.get("mpc_gain")
    summary_loss = extra_debug.get("mpc_loss")
    summary_horizon = extra_debug.get("mpc_horizon")
    summary_eval = extra_debug.get("mpc_eval_count")
    summary_cost = extra_debug.get("mpc_cost")
    summary_profile = debug.get("trv_profile")
    summary_profile_conf = debug.get("trv_profile_conf")
    summary_perf_bin = debug.get("perf_curve_bin")
    summary_perf_rate = debug.get("perf_room_rate")

    _LOGGER.debug(
        "better_thermostat %s: mpc calibration for %s: e0=%sK gain=%s loss=%s horizon=%s | "
        "raw=%s%% out=%s%% min_eff=%s%% last=%s%% dead_hits=%s eval=%s cost=%s | "
        "trv_profile=%s conf=%s perf_bin=%s perf_rate=%s",
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
        summary_profile,
        _round_for_debug(summary_profile_conf, 3),
        summary_perf_bin,
        _round_for_debug(summary_perf_rate, 4),
    )

    return MpcOutput(valve_percent=percent_out, debug=debug)


def _compute_predictive_percent(
    inp: MpcInput, params: MpcParams, state: _MpcState, now: float, delta_t: float
) -> tuple[float, dict[str, Any]]:
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

    # Determine which temperature to use for the cost function (Raw vs Filtered)
    # Default to filtered (EMA) for stability.
    current_temp_cost_C = current_temp_C
    temp_cost_source = "raw"

    if inp.filtered_temp_C is not None:
        try:
            current_temp_cost_C = float(inp.filtered_temp_C)
            temp_cost_source = "filtered"
        except (TypeError, ValueError):
            current_temp_cost_C = current_temp_C
            inp.filtered_temp_C = None

    use_virtual_temp = bool(getattr(params, "use_virtual_temp", True))

    # delta_t is kept for API/backward compatibility (pre-u0 versions used it)
    _ = delta_t

    if state.last_learn_time is None:
        state.last_learn_time = now
        state.last_learn_temp = current_temp_cost_C

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
        if state.solar_gain_est is None:
            state.solar_gain_est = getattr(params, "mpc_solar_gain_initial", 0.01)

    # Detect stale state (bucket switching): if this bucket wasn't updated for >15min,
    # reset learning anchors to avoid connecting old history with current state.
    if state.last_time > 0.0 and (now - state.last_time) > 900.0:
        state.last_learn_time = now
        state.last_learn_temp = current_temp_cost_C
        state.u_integral = 0.0
        state.time_integral = 0.0

    # Time since last measurement for adaptation
    dt_last = now - state.last_learn_time

    # Block adaptation shortly after a window-open event to avoid skewing gain/loss.
    window_block_s = float(getattr(params, "mpc_adapt_window_block_s", 0.0))
    if window_block_s > 0 and state.last_window_open_ts > 0:
        if now - state.last_window_open_ts < window_block_s:
            state.last_learn_time = now
            state.last_learn_temp = current_temp_cost_C
            dt_last = 0.0

    # Initialize ka_est if we have outdoor temp context
    if params.mpc_adapt and inp.outdoor_temp_C is not None and state.ka_est is None:
        # Default ka guess from current loss_est
        _loss = state.loss_est if state.loss_est is not None else params.mpc_loss_coeff
        _delta = max(5.0, float(current_temp_cost_C) - float(inp.outdoor_temp_C))
        state.ka_est = _loss / _delta

    # ---- ADAPTATION (rate-based identification) ----
    # Model: dT/dt ~= gain * u - loss, where gain/loss are in °C/min and u in [0..1]
    adapt_debug: dict[str, Any] = {}
    if params.mpc_adapt and state.last_learn_temp is not None and dt_last >= 180.0:
        try:
            if state.last_residual_time is None:
                state.last_residual_time = state.last_learn_time or now

            # Safety: residual window cannot be longer than the current stable period
            if (
                state.last_learn_time is not None
                and state.last_residual_time < state.last_learn_time
            ):
                state.last_residual_time = state.last_learn_time

            dt_residual = now - state.last_residual_time

            # Gate on target stability to avoid learning during setpoint steps.
            target_changed = False
            if state.last_target_C is not None:
                target_changed = (
                    abs(float(target_temp_C) - float(state.last_target_C)) >= 0.05
                )

            # Use time-weighted average valve position for learning
            if state.time_integral > 0:
                u_avg_pct = state.u_integral / state.time_integral
            else:
                u_avg_pct = (
                    state.last_percent if state.last_percent is not None else 0.0
                )

            u_last = max(0.0, min(100.0, float(u_avg_pct))) / 100.0

            dt_min = dt_last / 60.0
            if dt_min <= 0:
                dt_min = 0.0

            # measured temperature change (fallback) and rate estimate
            delta_T = float(current_temp_cost_C) - float(state.last_learn_temp)
            observed_rate = (delta_T / dt_min) if dt_min > 0 else 0.0  # °C/min
            observed_rate_delta = observed_rate
            rate_source = "delta"

            # If upstream provides a slope, prefer it for identification.
            # This helps with quantised sensors where delta_T stays at 0 for long periods.
            # DISABLED: Slope often lags behind reality (EMA), causing wrong learning signals.
            # We rely on the actual delta_T over the interval.
            # slope = inp.temp_slope_K_per_min
            # if slope is not None:
            #     try:
            #         observed_rate = float(slope)
            #         rate_source = "slope"
            #     except (TypeError, ValueError):
            #         pass

            implied_delta_T = observed_rate * dt_min if dt_min > 0 else 0.0

            # Learn only when the sensor actually changed (quantised sensors).
            temp_change_threshold_C = float(
                getattr(params, "mpc_temp_change_threshold_C", 0.05)
            )
            if temp_change_threshold_C <= 0:
                temp_change_threshold_C = 0.05
            temp_changed = abs(delta_T) >= temp_change_threshold_C

            # Slope logic disabled - we rely purely on actual temperature changes
            # to avoid learning from lagging EMA slopes.
            slope_rejected = False

            learn_signal = temp_changed

            # For quasi steady-state learning (loss residual, gain_ss), prefer the
            # delta-based rate unless we have an actual sensor change. This prevents
            # stale/quantised sensors with noisy slopes from biasing steady-state updates.
            observed_rate_ss = observed_rate
            if not temp_changed:
                observed_rate_ss = observed_rate_delta

            # sanity: avoid learning on extreme transients / sensor jumps
            # (typical indoor rate is far below 1°C/min)
            max_abs_rate = float(getattr(params, "mpc_max_abs_rate_C_per_min", 0.35))
            if max_abs_rate <= 0:
                max_abs_rate = 0.35
            rate_ok = abs(observed_rate) <= max_abs_rate

            if bool(getattr(params, "enable_min_effective_percent", True)):
                min_open = (state.min_effective_percent or 5.0) / 100.0
            else:
                min_open = 0.0

            # Identification safety: avoid learning gain/loss from tiny openings.
            # With min_open=0, u_last can be very small and would make gain_candidate
            # numerically unstable (division) and amplify noise.
            ident_min_u = 0.05

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
            loss_method: str | None = None
            gain_method: str | None = None
            gain_ss_applied = False
            gain_ss_rate_limited = False
            gain_ss_candidate: float | None = None

            # Common gates: don't learn during setpoint steps or crazy sensor jumps.
            common_ok = (not target_changed) and rate_ok and dt_min > 0

            # --- REGIME CHANGE DETECTION ---
            # Calculate predicted rate based on CURRENT model (before update)
            # predicted_rate = gain * u - loss
            predicted_rate = (gain_est * u_last) - loss_est

            # Prediction error (positive = room warmer than expected)
            pred_error = observed_rate - predicted_rate

            # Only track errors when conditions are stable (common_ok)
            if common_ok:
                state.recent_errors.append(pred_error)
                # deque(maxlen=20) handles eviction automatically

            # Check for regime change
            is_regime_change = _detect_regime_change(state.recent_errors)
            if is_regime_change and not state.regime_boost_active:
                state.regime_boost_active = True
                adapt_debug["regime_boost_activated"] = True

            # Calculate adaptive alpha
            base_adapt_alpha = params.mpc_adapt_alpha
            if state.regime_boost_active:
                # Boost alpha significantly (e.g. 3x) but cap at reasonable limit
                base_adapt_alpha = min(base_adapt_alpha * 3.0, 0.3)
                adapt_debug["regime_boost_active"] = True

                # Reset if detection returns False (bias gone or included in model now)
                if not is_regime_change:
                    state.regime_boost_active = False
                    adapt_debug["regime_boost_reset"] = True

            # --- LOSS learning: u ~= 0 and room cooling (or not warming) ---
            # Needs a real temperature change (quantised sensors).
            if (
                common_ok
                and learn_signal
                and u_last <= min_open
                and observed_rate < 0.0  # Allow learning even on slow cooling
            ):
                loss_candidate = max(0.0, -observed_rate)

                # Check for "Open Window" scenario:
                # If the observed cooling rate is significantly higher than the maximum allowed loss parameter,
                # it is likely an external disturbance (open window without sensor) rather than poor insulation.
                # Threshold: 1.5x max_loss (e.g. > 0.045 °C/min if max is 0.03).
                max_realistic_loss = params.mpc_loss_max * 1.5
                if loss_candidate > max_realistic_loss:
                    adapt_debug["loss_skipped_high_rate"] = True
                else:
                    loss_candidate = min(loss_candidate, params.mpc_loss_max)

                    alpha = base_adapt_alpha
                    if loss_candidate < loss_est:
                        alpha = base_adapt_alpha * 0.1  # slower decrease
                    state.loss_est = (1.0 - alpha) * loss_est + alpha * loss_candidate
                    updated_loss = True
                    loss_method = "cool_u0"
                    state.loss_learn_count += 1

            # --- LOSS learning (residual): works even when valves never close ---
            # Important: this must work even when delta_T == 0 (steady-state), so it
            # must NOT depend on temp_changed. Instead, gate on quasi steady-state.
            residual_ok = False
            residual_rate_limited = False
            residual_block_jump = False

            # Calculate u0_frac early for learning checks
            u0_frac_est = (loss_est / gain_est) if gain_est > 0 else 0.0
            u0_frac_est = max(0.0, min(1.0, u0_frac_est))

            if common_ok and (not updated_loss) and u_last > min_open:
                ss_rate_thr = 0.02  # °C/min: quasi steady-state threshold

                # Rate-limit residual learning using dt_residual window.
                residual_min_interval_s = 300.0  # 5min min window
                residual_max_interval_s = 3600.0  # 60min (avoid stale windows)
                if (
                    dt_residual < residual_min_interval_s
                    or dt_residual > residual_max_interval_s
                ):
                    residual_rate_limited = True
                if temp_changed:
                    residual_block_jump = True

                residual_ok = (
                    (not residual_rate_limited)
                    and (not residual_block_jump)
                    and (u_last >= ident_min_u)
                    # Allow learning within 10% absolute of u0.
                    # Relative % would be too strict for low u0 (e.g. 10% -> 12% limit),
                    # preventing correction of the base load.
                    and abs(u_last - u0_frac_est) <= 0.10
                    and abs(observed_rate_ss) <= ss_rate_thr
                )

                if residual_ok:
                    # Use current gain estimate; avoid division.
                    loss_candidate = (gain_est * u_last) - observed_rate_ss
                    loss_candidate = min(
                        max(loss_candidate, params.mpc_loss_min), params.mpc_loss_max
                    )

                    # Logic Check: "Insufficient Heat"
                    # If the room is significantly below target (e.g. >0.2K diff) but we are in steady state (rate ~ 0),
                    # it means the valve is open but not delivering enough heat.
                    # We should NOT increase the Loss estimate here (blaming insulation).
                    # Instead, we should reduce the Gain estimate (blaming the heater/flow) to open the valve more.
                    is_insufficient_heat = (
                        target_temp_C - current_temp_cost_C
                    ) > 0.2 and loss_candidate > loss_est

                    if not is_insufficient_heat:
                        # Heavily reduce alpha for steady-state learning to rely more on
                        # "cool_u0" events (forced calibration).
                        base_alpha = base_adapt_alpha * 0.2
                        alpha = base_alpha
                        if loss_candidate < loss_est:
                            alpha = base_alpha * 0.1  # slower decrease
                        state.loss_est = (
                            1.0 - alpha
                        ) * loss_est + alpha * loss_candidate
                        updated_loss = True
                        loss_method = "residual_u0_ss"
                        state.consecutive_insufficient_heat = 0
                    else:
                        adapt_debug["loss_skipped_insufficient_heat"] = True
                        state.consecutive_insufficient_heat += 1

                        # INSUFFICIENT HEAT BOOST
                        # If we have insufficient heat repeatedly, decrease Gain to boost u0 (and thus valve)
                        if state.consecutive_insufficient_heat >= 1:
                            # Use aggressive alpha for this correction
                            alpha_boost = max(0.1, base_adapt_alpha * 2.0)
                            # Reduce gain towards a lower target (e.g. 80% of current)
                            target_gain = gain_est * 0.8
                            target_gain = max(target_gain, params.mpc_gain_min)

                            state.gain_est = (
                                1.0 - alpha_boost
                            ) * gain_est + alpha_boost * target_gain
                            updated_gain = True
                            gain_method = "insufficient_heat_boost"
                            adapt_debug["gain_boosted_insuff"] = True

            # --- LOSS learning (warming with low valve): ---
            # If we are below u0 but the room is warming, loss is overestimated.
            # This handles the case where residual_u0_ss fails because rate is too high (warming).
            # GUARD: Only apply after gain has been validated by at least 2 heat_rate samples,
            # otherwise a wrong gain_est can push loss to min and create a positive feedback loop.
            if (
                common_ok
                and learn_signal
                and (not updated_loss)
                and state.gain_learn_count >= 2
                and u_last < (u0_frac_est - 0.05)
                and observed_rate > 0.0
            ):
                # We are warming, so gain*u > loss.
                # Since u is small, loss must be very small.
                loss_candidate = (gain_est * u_last) - observed_rate
                loss_candidate = min(
                    max(loss_candidate, params.mpc_loss_min), params.mpc_loss_max
                )

                # This will likely be negative or very small, driving loss down.
                # Reduce alpha to not overreact to solar gains etc.
                alpha = base_adapt_alpha * 0.5
                state.loss_est = (1.0 - alpha) * loss_est + alpha * loss_candidate
                updated_loss = True
                loss_method = "warm_low_u"

            # --- GAIN learning: u > min_open and room warming ---
            # Needs a real temperature change (quantised sensors).
            if (
                common_ok
                and learn_signal
                and (not updated_loss)
                and (u_last >= max(min_open, ident_min_u))
                and observed_rate > 0.001
            ):
                # gain = (observed_rate + loss) / u
                denom = max(u_last, 1e-3)
                gain_candidate = (observed_rate + loss_est) / denom

                gain_candidate = min(
                    max(gain_candidate, params.mpc_gain_min), params.mpc_gain_max
                )

                alpha = base_adapt_alpha
                if gain_candidate > gain_est:
                    alpha = base_adapt_alpha * 0.3  # slower increase
                state.gain_est = (1.0 - alpha) * gain_est + alpha * gain_candidate
                updated_gain = True
                gain_method = "heat_rate"
                state.gain_learn_count += 1

            # --- GAIN learning (steady-state high-u): ---
            # If we apply a high valve opening for a sustained period, temperature is
            # (almost) flat, but we're still below target, then the current gain is
            # likely overestimated. Correct gain downward so u0 (=loss/gain) can rise.
            if common_ok and u_last > max(min_open, 0.15):
                ss_min_interval_s = 300.0  # 5min
                ss_max_interval_s = 3600.0  # 60min
                if dt_residual < ss_min_interval_s or dt_residual > ss_max_interval_s:
                    gain_ss_rate_limited = True

                e_now = target_temp_C - float(inp.current_temp_C)

                if (
                    (not gain_ss_rate_limited)
                    and (not temp_changed)
                    and e_now > 0.1
                    and abs(observed_rate_ss) <= 0.01
                ):
                    denom = max(u_last, 0.05)
                    gain_ss_candidate = (max(0.0, observed_rate_ss) + loss_est) / denom
                    gain_ss_candidate = min(
                        max(gain_ss_candidate, params.mpc_gain_min), params.mpc_gain_max
                    )

                    gain_est_current = (
                        float(state.gain_est)
                        if state.gain_est is not None
                        else float(gain_est)
                    )

                    if gain_ss_candidate < gain_est_current:
                        alpha = base_adapt_alpha  # faster decrease is OK
                        state.gain_est = (1.0 - alpha) * gain_est_current + (
                            alpha * gain_ss_candidate
                        )
                        updated_gain = True
                        gain_method = "high_u_ss"
                        gain_ss_applied = True

            # --- GAIN RECOVERY: allow gain to recover after being driven down ---
            # If the room is warming faster than the model predicts AND the valve
            # is at a moderate opening, the gain was likely over-corrected downward.
            # Allow a slow upward correction (symmetric to the decrease paths).
            if (
                common_ok
                and learn_signal
                and (not updated_gain)
                and u_last >= ident_min_u
                and observed_rate > 0.01  # clear warming signal
            ):
                gain_implied = (observed_rate + loss_est) / max(u_last, 0.05)
                gain_implied = min(
                    max(gain_implied, params.mpc_gain_min), params.mpc_gain_max
                )
                gain_current = (
                    float(state.gain_est)
                    if state.gain_est is not None
                    else float(params.mpc_thermal_gain)
                )
                # Only recover upward if implied gain is significantly higher (>10%)
                if gain_implied > gain_current * 1.1:
                    alpha_recover = base_adapt_alpha * 0.2  # slow recovery
                    state.gain_est = (
                        1.0 - alpha_recover
                    ) * gain_current + alpha_recover * gain_implied
                    updated_gain = True
                    gain_method = "recovery"
                    adapt_debug["gain_recovery"] = True

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
                "id_implied_delta_T": _round_for_debug(implied_delta_T, 3),
                "id_temp_changed": temp_changed,
                "id_learn_signal": learn_signal,
                "id_temp_change_threshold_C": _round_for_debug(
                    temp_change_threshold_C, 3
                ),
                "id_rate": _round_for_debug(observed_rate, 4),
                "id_rate_delta": _round_for_debug(observed_rate_delta, 4),
                "id_rate_ss": _round_for_debug(observed_rate_ss, 4),
                "id_rate_source": rate_source,
                "id_slope_rejected": slope_rejected,
                "id_rate_ok": rate_ok,
                "id_u_last": _round_for_debug(u_last, 3),
                "id_target_changed": target_changed,
                "id_gain_method": gain_method,
                "id_gain_updated": updated_gain,
                "id_gain_ss_applied": gain_ss_applied,
                "id_gain_ss_candidate": (
                    _round_for_debug(gain_ss_candidate, 4)
                    if gain_ss_candidate is not None
                    else None
                ),
                "id_gain_ss_rate_limited": gain_ss_rate_limited,
                "id_loss_updated": updated_loss,
                "id_loss_method": loss_method,
                "id_loss_ss_rate_thr": _round_for_debug(0.02, 4),
                "id_residual_ok": residual_ok,
                "id_residual_rate_limited": residual_rate_limited,
                "id_residual_block_jump": residual_block_jump,
            }

            # Reset main anchor only on significant changes or context switch
            if temp_changed or target_changed:
                state.last_learn_time = now
                state.last_learn_temp = current_temp_cost_C
                state.u_integral = 0.0
                state.time_integral = 0.0

            # Track last residual/steady-state update to rate limit it separately
            if (updated_loss and loss_method == "residual_u0_ss") or (
                updated_gain and gain_method == "high_u_ss"
            ):
                state.last_residual_time = now

            # Update ka_est if loss was updated and we have context
            if updated_loss and inp.outdoor_temp_C is not None:
                _delta = max(
                    5.0, float(current_temp_cost_C) - float(inp.outdoor_temp_C)
                )
                _loss_val = (
                    state.loss_est
                    if state.loss_est is not None
                    else params.mpc_loss_coeff
                )
                state.ka_est = float(_loss_val) / _delta

        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # convert to per-step quantities (°C per simulation step)
    gain = state.gain_est if state.gain_est is not None else params.mpc_thermal_gain

    # Calculate base loss rate for u0 calculation
    if inp.outdoor_temp_C is not None and state.ka_est is not None:
        _current_delta = float(current_temp_cost_C) - float(inp.outdoor_temp_C)
        loss = float(state.ka_est) * _current_delta
        # Clamp to bounds to ensure u0 is sane
        loss = max(params.mpc_loss_min, min(params.mpc_loss_max, loss))
    else:
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
    # Subtract other heat power AND solar gain from requirements:
    # gain*u0 + other + solar = loss => gain*u0 = loss - other - solar
    effective_loss_for_u0 = (
        loss - float(inp.other_heat_power)
        # - (solar_gain_factor * float(getattr(inp, "solar_intensity", 0.0)))
    )

    if gain and gain > 0:
        u0_frac = effective_loss_for_u0 / gain
    else:
        u0_frac = 0.0
    u0_frac = max(0.0, min(1.0, u0_frac))
    # Only clamp baseline by learned min_effective_percent once we actually have evidence.
    if bool(getattr(params, "enable_min_effective_percent", True)):
        if (
            state.min_effective_percent is not None
            and state.min_effective_percent > 0.0
        ):
            min_eff = float(state.min_effective_percent) / 100.0
            u0_frac = max(u0_frac, min_eff)

    # cost penalty for control deviation from u0 (regularisation)
    control_pen = max(0.0, float(params.mpc_control_penalty))
    last_percent = state.last_percent if state.last_percent is not None else None

    # ----------------------------------------------------------------
    # ANALYTICAL OPTIMAL CONTROL (replaces coarse/fine brute-force)
    # ----------------------------------------------------------------
    # For the linear plant  T_{k+1} = T_k + gain_step * u - net_loss_step
    # with quadratic tracking cost  J = sum_{k=1..H} (T_target - T_k)^2
    #                                   + control_pen * (u - u0)^2
    # the optimal u has a closed-form solution.
    #
    # After H steps of constant u:
    #   T_k = T_0 + k * (gain_step * u + other_heat_step - loss_step)
    # Let  a = gain_step,  b = other_heat_step - loss_step,  e0 = target - T_0
    # Then error at step k:  e_k = e0 - k*(a*u + b)
    # Cost = sum_{k=1..H} (e0 - k*(a*u+b))^2 + control_pen*(u-u0)^2
    #
    # Setting dJ/du = 0 and solving for u gives the analytical optimum.
    # ----------------------------------------------------------------

    T0 = (
        float(state.virtual_temp)
        if use_virtual_temp and state.virtual_temp is not None
        else current_temp_cost_C
    )

    other_heat_step = float(inp.other_heat_power) * step_minutes

    # For constant-loss model (no ka_est or no outdoor temp)
    net_passive_step = other_heat_step - loss_step  # everything except gain*u

    e0 = target_temp_C - T0  # initial error (positive = need heating)

    a = gain_step  # temperature change per step per unit u

    # Summation constants:  S1 = sum(k, k=1..H),  S2 = sum(k^2, k=1..H)
    H = horizon
    S1 = H * (H + 1) / 2.0
    S2 = H * (H + 1) * (2 * H + 1) / 6.0

    # Analytical optimum:
    # u* = (a * S2)^-1 * [S1 * e0 - S2 * net_passive_step] + correction from control_pen
    # Full derivation:  dJ/du = -2*a*sum(k*(e0 - k*(a*u+b))) + 2*control_pen*(u-u0) = 0
    # => u = [a * (S1*e0 - S2*b) ] / (a^2 * S2 + control_pen)

    denom_analytical = a * a * S2 + control_pen
    if denom_analytical > 1e-12:
        u_star = (a * (S1 * e0 - S2 * net_passive_step)) / denom_analytical
    else:
        # gain is effectively zero; fall back to u0
        u_star = u0_frac

    # Clamp to [0, 1]
    u_star = max(0.0, min(1.0, u_star))

    # Compute cost at the analytical optimum for debug info
    cost_at_star = 0.0
    T_sim = T0
    for k in range(1, H + 1):
        T_sim = T0 + k * (a * u_star + net_passive_step)
        e_k = target_temp_C - T_sim
        cost_at_star += e_k * e_k
    cost_at_star += control_pen * (u_star - u0_frac) ** 2

    du_frac = u_star - u0_frac
    du_percent = du_frac * 100.0
    u_abs_percent = u_star * 100.0
    best_percent = u_abs_percent

    # store last estimates
    state.last_temp = (
        state.virtual_temp
        if use_virtual_temp and state.virtual_temp is not None
        else inp.current_temp_C
    )
    state.last_time = now

    # build debug
    mpc_debug = {
        "mpc_gain": _round_for_debug(gain, 4),
        "mpc_loss": _round_for_debug(loss, 4),
        "mpc_ka": _round_for_debug(state.ka_est, 5)
        if state.ka_est is not None
        else None,
        "mpc_u0_pct": _round_for_debug(u0_frac * 100.0, 3),
        "mpc_du_pct": _round_for_debug(du_percent, 3),
        "mpc_u_abs_pct": _round_for_debug(u_abs_percent, 3),
        "mpc_horizon": horizon,
        "mpc_eval_count": 0,
        "mpc_step_minutes": _round_for_debug(step_minutes, 3),
        "mpc_temp_cost_C": _round_for_debug(current_temp_cost_C, 3),
        "mpc_sensor_temp_C": _round_for_debug(inp.current_temp_C, 3),
        "mpc_temp_cost_source": temp_cost_source,
        "mpc_virtual_temp": (
            f"{state.virtual_temp:.3f}" if state.virtual_temp is not None else None
        ),
        "mpc_e0": _round_for_debug(e0, 3),
        "mpc_analytical": True,
    }

    if adapt_debug:
        mpc_debug.update(adapt_debug)

    mpc_debug["mpc_cost"] = _round_for_debug(cost_at_star, 6)

    if last_percent is not None:
        mpc_debug["mpc_last_percent"] = _round_for_debug(last_percent, 2)

    return best_percent, mpc_debug


def _detect_trv_profile(
    state: _MpcState,
    percent_out: float,
    temp_delta: float | None,
    time_delta: float | None,
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

    prev_profile = state.trv_profile
    prev_conf = state.profile_confidence

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

    if prev_profile != state.trv_profile or state.profile_confidence != prev_conf:
        _LOGGER.debug(
            "better_thermostat: TRV profile update profile=%s conf=%s samples=%s resp_ratio=%s percent=%s",
            state.trv_profile,
            _round_for_debug(state.profile_confidence, 3),
            state.profile_samples,
            _round_for_debug(response_ratio, 3),
            _round_for_debug(percent_out, 1),
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
    delta_t: float | None,
) -> tuple[int, dict[str, Any], float | None]:
    """Apply smoothing, hysteresis, min-effective, du_max, dead-zone detection and produce debug info."""

    name = inp.bt_name or "BT"
    entity = inp.entity_id or "unknown"

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
    if bool(getattr(params, "enable_min_effective_percent", True)):
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
    if bool(getattr(params, "enable_min_effective_percent", True)):
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
    temp_delta: float | None = None
    time_delta: float | None = None

    if inp.trv_temp_C is None:
        state.last_trv_temp = None
        state.last_trv_temp_ts = 0.0
        state.dead_zone_hits = 0
    elif state.last_trv_temp is None or state.last_trv_temp_ts == 0.0:
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
                state, percent_out, temp_delta, time_delta, expected_temp_rise, params
            )

            if bool(getattr(params, "enable_min_effective_percent", True)):
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
                state.dead_zone_hits = 0

        else:
            # deadzone fully disabled because TRV profile is known
            pass

        state.last_trv_temp = inp.trv_temp_C
        state.last_trv_temp_ts = now
    # 7) DEBUG INFO
    # ============================================================
    debug: dict[str, Any] = {
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
        "mpc_created_ts": state.created_ts,
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
            # Only bypass hold-time for large OPENING steps (e.g. window just closed / sudden demand).
            # Large closing steps should remain rate-limited to avoid oscillations.
            big_open = (smooth - state.last_percent) >= params.big_change_force_open_pct
            remaining = hold_time - time_since_update

            if remaining > 0.0 and time_since_update < hold_time and not big_open:
                percent_out = int(round(state.last_percent))
                debug["hold_block"] = True
                debug["hold_remaining_s"] = int(max(0.0, remaining))
                _LOGGER.debug(
                    "better_thermostat %s: MPC hold-time block (%s) last_percent=%s output=%s remaining_s=%s",
                    name,
                    entity,
                    _round_for_debug(state.last_percent, 2),
                    percent_out,
                    _round_for_debug(remaining, 1),
                )

    # ============================================================
    # 7b) MAX VALVE OPENING (USER CAP)
    # ============================================================
    max_opening = getattr(inp, "max_opening_pct", None)
    if isinstance(max_opening, (int, float)):
        max_opening = max(0.0, min(100.0, float(max_opening)))
        if percent_out > max_opening:
            debug["max_opening_pct"] = _round_for_debug(max_opening, 2)
            debug["max_opening_clamped"] = True
            percent_out = int(round(max_opening))

    # ============================================================
    # 8) UPDATE STATE ONLY IF CHANGED
    # ============================================================
    # Only update last_percent and last_update_ts if the output actually changed
    original_last_percent = state.last_percent
    if original_last_percent is None or abs(percent_out - original_last_percent) >= 0.5:
        state.last_percent = float(percent_out)
        state.last_update_ts = now

    return percent_out, debug, delta_t
