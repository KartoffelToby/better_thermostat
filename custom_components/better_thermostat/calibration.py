"""Helper functions for the Better Thermostat component."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
import logging
import math
from typing import TYPE_CHECKING

from homeassistant.components.climate.const import HVACAction, HVACMode

from custom_components.better_thermostat.core.calibrator import CalibratorHealth
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.model_fixes.model_quirks import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    local_calibration_shifts_setpoint,
)
from custom_components.better_thermostat.utils.advanced_flags import advanced_flag
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcOutput,
    MpcParams,
    build_mpc_group_key,
    build_mpc_key,
    compute_mpc,
    distribute_valve_percent,
    sanitize_mpc_state,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Output,
    MpcV2Params,
    PlantParams,
    ReidOutcome,
    ReidSample,
    compute_mpc_v2,
    make_plant_prior,
    run_reid_fit,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    build_pid_key,
    build_pid_loop_key,
    compute_pid,
    observe_standby as pid_observe_standby,
    pid_cycle_params,
    pid_cycle_state,
    pid_loop_state,
    resolve_unique_id,
    sanitize_pid_state,
    settle_pid_cycle,
)
from custom_components.better_thermostat.utils.calibration.strategies import (
    BalanceCalibrator,
    BalanceStrategy,
    ChannelAdjustment,
    ModeTraits,
    annunciate_health,
    build_strategy_registry,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiOutput,
    TpiParams,
    build_tpi_key,
    compute_tpi,
    sanitize_tpi_state,
)
from custom_components.better_thermostat.utils.const import (
    CONF_MPC_V2_PLANT_PRESET,
    CONF_PROTECT_OVERHEATING,
    CalibrationMode,
    CalibrationOutput,
    MpcV2PlantPreset,
)
from custom_components.better_thermostat.utils.helpers import (
    Rounding,
    clamp_valve_percent,
    configured_calibration_mode,
    configured_calibration_output,
    convert_to_float,
    convert_to_float_celsius,
    heating_power_valve_position,
    normalize_step,
    published_setpoint_grid,
    round_by_step,
)
from custom_components.better_thermostat.utils.state_manager import MpcV2ReidData
from custom_components.better_thermostat.utils.watcher import reachable_trv_temperature

if TYPE_CHECKING:
    from custom_components.better_thermostat.climate import BetterThermostat
    from custom_components.better_thermostat.trv import Trv

_LOGGER = logging.getLogger(__name__)

# Offline re-identification cadence: at most one fit attempt per key in this
# interval, and only once the sample buffer holds enough history to plausibly
# contain full transients (240 samples ≈ 4-20 h depending on cycle rate).
_MPC_V2_REID_INTERVAL_S = 6 * 3600.0
_MPC_V2_REID_MIN_SAMPLES = 240

# Post-adjustment gates that fire because Better Thermostat is not calling for
# heat take the same arm while the cooler runs: a TRV valve that opens works
# against the cooler, so cooling is the strongest form of not calling for heat.
_IDLE_OR_COOLING = (HVACAction.IDLE, HVACAction.COOLING)


def _compute_zero_open_offset(
    self: BetterThermostat,
    entity_id: str,
    _cur_trv_temperature: float,
    _cur_external_temperature: float,
    _cur_target_temperature: float,
    _trv_temperature_step: float,
) -> float:
    """Compute the offset to push setpoint below TRV temperature when valve fraction is zero.

    Returns the offset so that callers can set:
        _calibrated_setpoint = _cur_trv_temperature - setpoint_drop
    """
    _overshoot = max(0.0, _cur_external_temperature - _cur_target_temperature)
    _trv_min_temperature = convert_to_float(
        str(self.real_trvs[entity_id].min_temp),
        self.device_name,
        "_compute_zero_open_offset()",
    )
    _max_setpoint_drop = (
        max(1.0, _cur_trv_temperature - float(_trv_min_temperature))
        if _trv_min_temperature is not None
        else 8.0
    )
    _setpoint_drop = _max_setpoint_drop * (1.0 - math.exp(-0.5 * _overshoot))
    _setpoint_drop = max(_trv_temperature_step, _setpoint_drop)
    return _setpoint_drop


def effective_room_temperature(self: BetterThermostat) -> float | None:
    """Room temperature for the control law, honoring the fail-soft ladder.

    Under SENSOR_FALLBACK the mean of the internal temperatures of the
    reachable TRVs substitutes the (dead) room sensor — completing the
    fallback that the watcher has always announced. On every other rung
    this is simply the current room temperature.

    Parameters
    ----------
    self :
        BetterThermostat entity instance.

    Returns
    -------
    float | None
        Effective room temperature, or ``None`` when no usable reading
        exists.
    """
    mode = self.kernel_state.control_mode.mode
    if mode == ControlMode.SENSOR_FALLBACK:
        temps = [
            value
            for entity_id in self.real_trvs
            if (value := reachable_trv_temperature(self, entity_id)) is not None
        ]
        if temps:
            return sum(temps) / len(temps)
    return self.room_temperature


def _get_current_outdoor_temperature(self: BetterThermostat) -> float | None:
    """Get current outdoor temperature from outdoor sensor or weather entity."""
    if self.outdoor_sensor_entity_id is not None:
        state = self.hass.states.get(self.outdoor_sensor_entity_id)
        if state:
            return convert_to_float_celsius(
                state.state,
                self.device_name,
                "_get_current_outdoor_temperature()",
                unit_of_measurement=state.attributes.get("unit_of_measurement"),
            )

    if self.weather_entity_id is not None:
        state = self.hass.states.get(self.weather_entity_id)
        if state and state.attributes:
            return convert_to_float_celsius(
                state.attributes.get("temperature"),
                self.device_name,
                "_get_current_outdoor_temperature()",
                unit_of_measurement=state.attributes.get("temperature_unit"),
            )

    return None


def _get_solar_context(self: BetterThermostat) -> tuple[bool, float]:
    """Daylight flag plus current solar intensity (0.0 below the horizon)."""
    is_day = True
    if self.hass is not None:
        sun = self.hass.states.get("sun.sun")
        if sun is not None and sun.state == "below_horizon":
            is_day = False
    return is_day, (_get_current_solar_intensity(self) if is_day else 0.0)


def _get_current_solar_intensity(self: BetterThermostat) -> float:
    """Estimate solar intensity (0.0 to 1.0) based on weather entity data."""
    if self.weather_entity_id is None:
        return 0.0

    state = self.hass.states.get(self.weather_entity_id)
    if not state or not state.attributes:
        return 0.0

    def _get_val(data: object, key: str) -> float | int | str | None:
        if not isinstance(data, dict):
            return None
        return data.get(key)

    # Prepare data sources: Attributes, and optionally the first Forecast
    sources = [state.attributes]

    # Check forecast if available (common in many weather integrations)
    forecast = state.attributes.get("forecast")
    if isinstance(forecast, list) and len(forecast) > 0:
        # We take the first forecast item as it's typically the current or next hour
        sources.append(forecast[0])

    # 1. Cloud coverage (0-100) -> Lower is better
    for source in sources:
        cc = _get_val(source, "cloud_coverage")
        if cc is not None:
            try:
                # 0% clouds = 1.0 intensity, 100% clouds = 0.0 intensity
                return max(0.0, min(1.0, (100.0 - float(cc)) / 100.0))
            except ValueError, TypeError, OverflowError:
                pass

    # 2. UV Index (0-10+) -> Higher is better
    for source in sources:
        uv = _get_val(source, "uv_index")
        if uv is not None:
            try:
                # Normalize UV index (approx 0-10 range)
                return max(0.0, min(1.0, float(uv) / 10.0))
            except ValueError, TypeError, OverflowError:
                pass

    # 3. Weather condition mapping
    # 'sunny', 'clear-night' -> High potential (during day)
    # 'partlycloudy' -> Medium
    # 'cloudy', 'fog', 'rain', etc. -> Low
    condition = state.state
    # If state is numeric or unknown, try condition from forecast
    if condition in (None, "unknown", "") and len(sources) > 1:
        condition = _get_val(sources[1], "condition")

    if condition in ("sunny", "clear", "clear-night", "windy", "exceptional"):
        return 1.0
    if condition in ("partlycloudy",):
        return 0.7
    if condition in ("cloudy",):
        return 0.4

    return 0.1  # Default low for rain/snow/fog etc


def _supports_direct_valve_control(self: BetterThermostat, entity_id: str) -> bool:
    """Return True if the TRV supports writing a valve percentage."""

    trv = self.real_trvs[entity_id]
    if (
        configured_calibration_output(trv.advanced)
        != CalibrationOutput.DIRECT_VALVE_BASED
    ):
        return False
    return trv.capabilities().supports_valve_write


def _get_trv_max_opening(self: BetterThermostat, entity_id: str) -> float:
    """Return the user-defined max opening percent for a TRV."""
    return max(0.0, min(100.0, float(self.real_trvs[entity_id].valve_max_opening)))


def _heating_power_adjustment(
    self: BetterThermostat,
    entity_id: str,
    current_value: float,
    *,
    hold_value: float,
    legacy_fallback: Callable[[float], float],
) -> tuple[float, bool]:
    """Shared HEATING_POWER machinery for both calibration channels.

    When direct valve control is available, publish the valve intent
    (closed while not heating, the heating-power position otherwise) and
    hold the channel value so the calibration does not counteract the
    valve command. Without valve support, fall back to the channel's
    legacy valve-position math. Without a room temperature or a target
    the valve cannot be sized, and the channel keeps ``current_value``.

    Returns ``(value, skip_post_adjustments)``.
    """
    trv = self.real_trvs[entity_id]

    if self.hvac_action != HVACAction.HEATING:
        if _supports_direct_valve_control(self, entity_id):
            trv.calibration_balance = {
                "valve_percent": 0,
                "apply_valve": True,
                "debug": {"source": "heating_power_calibration"},
            }
            return hold_value, True
        trv.calibration_balance = None
        return current_value, False

    # The position is bounded to 0..1, so the percentage is always finite.
    _valve_position = heating_power_valve_position(
        self, entity_id, effective_room_temperature(self)
    )
    if _valve_position is None:
        # Without a room temperature or a target there is no demand to size
        # the valve from, so the channel keeps its base calibration.
        trv.calibration_balance = None
        return current_value, False
    if _supports_direct_valve_control(self, entity_id):
        trv.calibration_balance = {
            "valve_percent": clamp_valve_percent(_valve_position * 100.0),
            "apply_valve": True,
            "debug": {"source": "heating_power_calibration"},
        }
        return hold_value, True

    trv.calibration_balance = None
    return legacy_fallback(_valve_position), False


def _collect_trv_temps_and_warmest(
    real_trvs: Mapping[str, Trv], fallback_id: str
) -> tuple[dict[str, float | None], str]:
    """Return per-TRV temperatures and the id of the warmest TRV.

    TRVs whose reading is missing or non-numeric map to ``None``; when no
    TRV has a usable reading the warmest id falls back to ``fallback_id``.
    """
    trv_temps: dict[str, float | None] = {}
    warmest_trv_id = fallback_id
    warmest_temperature: float | None = None
    for eid, tdata in real_trvs.items():
        _t = tdata.current_temperature
        if _t is None:
            trv_temps[eid] = None
            continue
        temperature_value = float(_t)
        trv_temps[eid] = temperature_value
        if warmest_temperature is None or temperature_value > warmest_temperature:
            warmest_temperature = temperature_value
            warmest_trv_id = eid
    return trv_temps, warmest_trv_id


def _compute_mpc_balance(
    self: BetterThermostat, entity_id: str
) -> tuple[MpcOutput | None, bool]:
    """Run the MPC balance algorithm for calibration purposes.

    When the BT instance controls **multiple TRVs**, a single shared MPC model
    is evaluated once (using the room-level external sensor) and the resulting
    valve command is distributed across TRVs proportional to their internal
    temperature deficit.  A cold TRV (low ``current_temperature``) receives
    *more* valve opening; a warm one receives *less*.

    With a **single TRV** there is no distribution step: the model is keyed
    per entity and its valve command is applied as computed.
    """

    trv_state = self.real_trvs[entity_id]

    mpc_room_temperature = effective_room_temperature(self)
    if self.heat_target_temperature is None or mpc_room_temperature is None:
        trv_state.calibration_balance = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state.calibration_balance = None
        return None, False

    is_multi_trv = len(self.real_trvs) > 1

    trv_temps: dict[str, float | None] | None = None
    warmest_trv_id = entity_id
    if is_multi_trv:
        trv_temps, warmest_trv_id = _collect_trv_temps_and_warmest(
            self.real_trvs, entity_id
        )

    max_opening_percent = _get_trv_max_opening(
        self, warmest_trv_id if is_multi_trv else entity_id
    )

    params = MpcParams()

    # Optional: use filtered external temperature for MPC cost evaluation to reduce jitter.
    # `room_temperature_filtered` is maintained by events/temperature.py (EMA) and passed separately.
    mpc_room_temperature_filtered = (
        self.room_temperature_filtered
        if mpc_room_temperature is self.room_temperature
        else None
    )

    _is_day, _solar_intensity = _get_solar_context(self)

    # Use a group key for multi-TRV setups so all TRVs share one MPC model.
    if is_multi_trv:
        mpc_key = build_mpc_group_key(self)
    else:
        mpc_key = build_mpc_key(self, entity_id)

    state_mgr = self.state_mgr
    if state_mgr is None:
        trv_state.calibration_balance = None
        return None, False

    mpc_state = state_mgr.get_mpc(mpc_key)

    # Self-heal a poisoned state before it reaches the controller; the
    # verdict is annunciated on the TRV.
    mpc_state, _mpc_health = sanitize_mpc_state(mpc_state)
    annunciate_health(self, entity_id, _mpc_health)

    try:
        mpc_output, mpc_state = compute_mpc(
            MpcInput(
                key=mpc_key,
                target_temperature=self.heat_target_temperature,
                room_temperature=mpc_room_temperature,
                room_temperature_filtered=mpc_room_temperature_filtered,
                trv_temperature=trv_state.current_temperature,
                tolerance_K=float(self.tolerance or 0.0),
                temperature_slope_K_per_min=self.temperature_slope,
                window_open=self.contact_open,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
                outdoor_temperature=_get_current_outdoor_temperature(self),
                is_day=_is_day,
                solar_intensity=_solar_intensity,
                max_opening_percent=max_opening_percent,
            ),
            params,
            state=mpc_state,
            all_states=state_mgr.state.mpc,
        )
        state_mgr.set_mpc(mpc_key, mpc_state)
    except (ValueError, TypeError, ZeroDivisionError) as err:
        # A healed (sanitized) state must reach the store even when the
        # compute fails, otherwise the poisoned version stays on disk and
        # is re-healed every cycle.
        if _mpc_health != CalibratorHealth.HEALTHY:
            state_mgr.set_mpc(mpc_key, mpc_state)
        _LOGGER.debug(
            "better_thermostat %s: MPC calibration compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state.calibration_balance = None
        return None, False

    if mpc_output is None:
        trv_state.calibration_balance = None
        return None, False

    group_valve_percent = float(mpc_output.valve_percent)

    # --- Multi-TRV distribution ---
    if is_multi_trv:
        trv_temps = trv_temps or {}
        distributed = distribute_valve_percent(
            u_total_percent=group_valve_percent, trv_temps=trv_temps
        )
        this_trv_percent = distributed.get(entity_id, group_valve_percent)

        _LOGGER.debug(
            "better_thermostat %s: MPC grouped distribution for %s: "
            "group_pct=%.1f%% → this_trv_pct=%.1f%% | trv_temps=%s → distributed=%s",
            self.device_name,
            entity_id,
            group_valve_percent,
            this_trv_percent,
            {k: round(v, 1) if v is not None else None for k, v in trv_temps.items()},
            {k: round(v, 1) for k, v in distributed.items()},
        )
    else:
        this_trv_percent = group_valve_percent

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state.calibration_balance = {
        "valve_percent": clamp_valve_percent(this_trv_percent),
        "apply_valve": supports_valve,
        "debug": {
            **mpc_output.debug,
            "group_valve_pct": group_valve_percent,
            "distributed_valve_pct": this_trv_percent,
        },
    }

    self.schedule_save_state()

    # Return an MpcOutput-like object with the TRV-specific valve_percent
    trv_output = replace(
        mpc_output, valve_percent=clamp_valve_percent(this_trv_percent)
    )

    return trv_output, supports_valve


def _build_mpc_v2_reid_key(self: BetterThermostat) -> str:
    """Return the target-independent re-identification key for this BT.

    The plant prior describes the room, not an operating point, so the
    re-ID buffer, its fit cadence, and the adopted result are shared
    across all target-temperature buckets and all TRVs of a group.
    """
    uid = resolve_unique_id(self)
    return f"{uid}:reid"


def _lookup_mpc_v2_reid(
    self: BetterThermostat, reid_key: str, mpc_key: str
) -> MpcV2ReidData | None:
    """Return the adopted re-ID result, preferring the shared key.

    Results persisted under per-target-bucket keys (``uid:entity:tX.X`` or
    ``uid:group:tX.X``) remain readable: when nothing is stored under the
    shared key yet, the freshest bucket entry for this entity (or group)
    still seeds the prior until a fit is adopted under the shared key.
    """
    state_mgr = self.state_mgr
    if state_mgr is None:
        return None
    result = state_mgr.get_mpc_v2_reid(reid_key)
    if result is not None:
        return result
    uid_entity = mpc_key.rsplit(":", 1)[0]
    candidates = [
        data
        for key, data in state_mgr.state.mpc_v2_reid.items()
        if key.rsplit(":", 1)[0] == uid_entity
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda data: data.fitted_ts)


def _record_mpc_v2_reid_sample(
    self: BetterThermostat,
    reid_key: str,
    *,
    applied_valve_percent: float | None,
    trv_temperature: float | None,
    outdoor_temperature: float | None,
) -> None:
    """Append one observation to the re-identification buffer for a key.

    The buffer's spacing floor collapses the per-TRV dispatches of a group
    pass into a single sample, and its finiteness checks reject anything a
    later fit could choke on. Open-contact samples (window or door) are
    recorded on purpose: they never enter a fit but cut segments at the
    right place.

    Sampling is gated to the OPTIMAL rung of the fail-soft ladder: under
    SENSOR_FALLBACK ``room_temperature`` freezes at the last valid reading while
    the valve keeps moving, so a recorded sample would pair a frozen
    temperature with live valve activity and bias the tau/gain fit (the
    holdout is drawn from the same buffer and cannot catch this). The
    resulting recording pause leaves a time gap that the segmenter cuts
    on (``ReidConfig.max_gap_s``), keeping transients on either side of
    a degraded episode separate.
    """
    if self.kernel_state.control_mode.mode != ControlMode.OPTIMAL:
        return
    state_mgr = self.state_mgr
    if state_mgr is None:
        return
    room_temperature = self.room_temperature
    if room_temperature is None:
        return
    t_room = float(room_temperature)
    if applied_valve_percent is None:
        # A current MPC proposal is not evidence of a physical valve input:
        # the write can still be deferred, clamped, or fail.
        return
    u_frac = applied_valve_percent / 100.0
    if not math.isfinite(u_frac):
        return
    u_frac = max(0.0, min(1.0, u_frac))
    runtime = state_mgr.get_mpc_v2_reid_runtime(reid_key)
    runtime.buffer.append(
        ReidSample(
            t_s=self.clock.monotonic(),
            T_room=t_room,
            u_frac=u_frac,
            T_outdoor=outdoor_temperature,
            T_trv=trv_temperature
            if isinstance(trv_temperature, (int, float))
            else None,
            window_open=self.contact_open,
        )
    )


def _confirmed_valve_percent(trv_state: Trv | None) -> float | None:
    """Return the best known prior valve input for model feedback.

    A device-reported position wins when available.  Otherwise the adapter's
    ``last_valve_percent`` is suitable: it is updated only after its write
    routine returned success.  No value means "unknown", not "closed".
    """
    for value in (
        trv_state.valve_position if trv_state is not None else None,
        trv_state.last_valve_percent if trv_state is not None else None,
    ):
        if value is None:
            continue
        try:
            percent = float(value)
        except TypeError, ValueError:
            continue
        if math.isfinite(percent) and 0.0 <= percent <= 100.0:
            return percent
    return None


def _maybe_start_mpc_v2_reid_fit(
    self: BetterThermostat, reid_key: str, v2_params: MpcV2Params
) -> None:
    """Kick off an offline re-identification fit in the executor when due.

    At most one attempt per key per ``_MPC_V2_REID_INTERVAL_S``, only once
    the buffer plausibly contains full transients, and never concurrently.
    The fit itself is pure CPU work; an accepted result is adopted through
    the state manager's bumpless path on the completion callback, so the
    event loop never blocks on the optimisation. Adoption bumplessly
    transfers every cached live controller; the result itself is stored
    under ``reid_key``.
    """
    hass = self.hass
    if hass is None:
        return
    state_mgr = self.state_mgr
    if state_mgr is None:
        return
    runtime = state_mgr.get_mpc_v2_reid_runtime(reid_key)
    # Monotonic for the cadence gate and buffer timing (immune to wall-clock
    # jumps); a separate wall-clock stamp records *when* an accepted fit
    # happened for the persisted result and diagnostics.
    now = self.clock.monotonic()
    if runtime.fit_inflight:
        return
    if now - runtime.last_fit_attempt_ts < _MPC_V2_REID_INTERVAL_S:
        return
    if len(runtime.buffer.samples) < _MPC_V2_REID_MIN_SAMPLES:
        return
    runtime.last_fit_attempt_ts = now
    runtime.fit_inflight = True
    fitted_wall_ts = self.clock.now().timestamp()
    # Snapshot the buffer so the executor thread never races the event
    # loop's appends; the current prior is the validation baseline.
    samples = list(runtime.buffer.samples)
    prior = v2_params.plant
    device_name = self.device_name
    schedule_save = self.schedule_save_state

    def _on_fit_done(future: asyncio.Future[ReidOutcome]) -> None:
        runtime.fit_inflight = False
        try:
            outcome = future.result()
        except Exception as err:
            _LOGGER.warning(
                "better_thermostat %s: MPC v2 re-identification failed for %s: %s",
                device_name,
                reid_key,
                err,
                exc_info=True,
            )
            return
        # The fit runs in the executor and nothing cancels it on removal. A
        # result arriving after the removal belongs to a thermostat whose
        # final save is already written; adopting it would only hand its
        # store a write after that.
        if self.is_removed:
            return
        if (
            outcome.status == "accepted"
            and outcome.tau_room_min is not None
            and outcome.gain_heater is not None
        ):
            state_mgr.adopt_mpc_v2_reid(
                reid_key,
                MpcV2ReidData(
                    tau_room_minutes=outcome.tau_room_min,
                    gain_heater=outcome.gain_heater,
                    fitted_ts=fitted_wall_ts,
                    rmse_prior_kelvin=outcome.rmse_prior_K or 0.0,
                    rmse_fit_kelvin=outcome.rmse_fit_K or 0.0,
                    n_segments=outcome.n_segments,
                ),
            )
            schedule_save()
            _LOGGER.info(
                "better_thermostat %s: adopted re-identified MPC v2 plant prior "
                "for %s: tau_room=%.0f min gain=%.2f (holdout RMSE %.3f -> %.3f K, "
                "%d segments)",
                device_name,
                reid_key,
                outcome.tau_room_min,
                outcome.gain_heater,
                outcome.rmse_prior_K or 0.0,
                outcome.rmse_fit_K or 0.0,
                outcome.n_segments,
            )
        else:
            _LOGGER.debug(
                "better_thermostat %s: MPC v2 re-identification for %s: %s "
                "(segments=%d, samples=%d)",
                device_name,
                reid_key,
                outcome.status,
                outcome.n_segments,
                outcome.n_samples,
            )

    try:
        future = hass.async_add_executor_job(run_reid_fit, samples, prior)
    except Exception as err:
        # Submitting the job failed before it could run; clear the guard so a
        # later cycle can retry instead of blocking fits forever.
        runtime.fit_inflight = False
        _LOGGER.warning(
            "better_thermostat %s: could not schedule MPC v2 re-identification "
            "for %s: %s",
            device_name,
            reid_key,
            err,
            exc_info=True,
        )
        return
    future.add_done_callback(_on_fit_done)


def _compute_mpc_v2_balance(
    self: BetterThermostat, entity_id: str
) -> tuple[MpcV2Output | None, bool]:
    """Run the MPC v2 (QP + Kalman) balance algorithm.

    Routes through ``compute_mpc_v2`` so the receding-horizon QP controller
    produces the valve recommendation. Multi-TRV setups use the shared
    :func:`distribute_valve_percent` helper — the controller only ever sees
    the group-level signal.
    """
    trv_state = self.real_trvs[entity_id]

    mpc_room_temperature = effective_room_temperature(self)
    if self.heat_target_temperature is None or mpc_room_temperature is None:
        trv_state.calibration_balance = None
        return None, False

    if self.bt_hvac_mode == HVACMode.OFF:
        trv_state.calibration_balance = None
        return None, False

    state_mgr = self.state_mgr
    if state_mgr is None:
        trv_state.calibration_balance = None
        return None, False

    is_multi_trv = len(self.real_trvs) > 1
    trv_temps: dict[str, float | None] | None = None
    warmest_trv_id = entity_id
    if is_multi_trv:
        trv_temps, warmest_trv_id = _collect_trv_temps_and_warmest(
            self.real_trvs, entity_id
        )

    max_opening_percent = _get_trv_max_opening(
        self, warmest_trv_id if is_multi_trv else entity_id
    )

    if is_multi_trv:
        mpc_key = build_mpc_group_key(self)
    else:
        mpc_key = build_mpc_key(self, entity_id)

    # In a group all TRVs share one controller state, so the plant preset must
    # be group-stable: derive it from a deterministic representative (first TRV
    # by entity_id) rather than whichever TRV is currently being dispatched,
    # otherwise mixed per-TRV presets re-parameterize the shared controller.
    preset_source_id = min(self.real_trvs) if is_multi_trv else entity_id
    advanced = self.real_trvs[preset_source_id].advanced or {}
    preset_raw = advanced.get(CONF_MPC_V2_PLANT_PRESET, MpcV2PlantPreset.AUTO)
    try:
        preset = MpcV2PlantPreset(preset_raw)
    except ValueError:
        preset = MpcV2PlantPreset.AUTO

    # Plant-prior resolution: an explicit preset always wins; under AUTO a
    # validated offline re-identification result beats the heat-loss
    # heuristic, which remains the fallback until the first accepted fit.
    reid_key = _build_mpc_v2_reid_key(self)
    reid_result = (
        _lookup_mpc_v2_reid(self, reid_key, mpc_key)
        if preset == MpcV2PlantPreset.AUTO
        else None
    )
    if reid_result is not None:
        plant_prior = PlantParams(
            tau_room_min=reid_result.tau_room_minutes,
            gain_heater=reid_result.gain_heater,
        )
    else:
        plant_prior = make_plant_prior(
            heating_power=self.heating_power,
            heat_loss_rate=self.heat_loss_rate,
            preset=None if preset == MpcV2PlantPreset.AUTO else preset.value,
        )
    v2_params = MpcV2Params(plant=plant_prior)

    outdoor_temperature = _get_current_outdoor_temperature(self)
    # The single-TRV path has one physical input.  A group controller's
    # distributed outputs are intentionally not collapsed into a fictional
    # single valve fraction; it keeps its optimistic command until group
    # actuator aggregation has an explicit plant contract.
    confirmed_valve_percent = (
        None if is_multi_trv else _confirmed_valve_percent(trv_state)
    )
    # The controller's applied input is the anchor of its rate limit and of
    # the smoothing its next plan weighs, so it may only be an opening BT
    # wrote itself. A TRV steered through its setpoint opens by its own
    # regulator: fed back, that opening would pull BT's next plan towards it,
    # raise the setpoint and open the TRV further. The re-identification
    # samples below keep the reported opening, which there is a measurement
    # of the room's input.
    controller_applied_percent = (
        confirmed_valve_percent
        if _supports_direct_valve_control(self, entity_id)
        else None
    )

    try:
        mpc_v2_state = state_mgr.get_mpc_v2_live(mpc_key, v2_params)
        mpc_output, mpc_v2_state = compute_mpc_v2(
            MpcV2Input(
                key=mpc_key,
                target_temperature=self.heat_target_temperature,
                room_temperature=mpc_room_temperature,
                trv_temperature=trv_state.current_temperature,
                window_open=self.contact_open,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
                outdoor_temperature=outdoor_temperature,
                max_opening_percent=max_opening_percent,
                applied_valve_percent=controller_applied_percent,
            ),
            v2_params,
            state=mpc_v2_state,
        )
    except (ValueError, TypeError, ZeroDivisionError) as err:
        _LOGGER.debug(
            "better_thermostat %s: MPC v2 compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state.calibration_balance = None
        return None, False

    state_mgr.set_mpc_v2_live(mpc_key, mpc_v2_state)

    if preset == MpcV2PlantPreset.AUTO and not is_multi_trv:
        _record_mpc_v2_reid_sample(
            self,
            reid_key,
            applied_valve_percent=confirmed_valve_percent,
            trv_temperature=trv_state.current_temperature,
            outdoor_temperature=outdoor_temperature,
        )
        _maybe_start_mpc_v2_reid_fit(self, reid_key, v2_params)

    if mpc_output is None:
        trv_state.calibration_balance = None
        return None, False

    group_valve_percent = float(mpc_output.valve_percent)

    if is_multi_trv:
        trv_temps = trv_temps or {}
        distributed = distribute_valve_percent(
            u_total_percent=group_valve_percent, trv_temps=trv_temps
        )
        this_trv_percent = distributed.get(entity_id, group_valve_percent)
    else:
        this_trv_percent = group_valve_percent

    # The controller only sees the group-level cap (warmest TRV); the
    # distribution can boost a colder TRV above its own configured limit,
    # so each per-TRV command is clamped to that TRV's max opening here.
    this_trv_percent = min(this_trv_percent, _get_trv_max_opening(self, entity_id))

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state.calibration_balance = {
        "valve_percent": round(max(0.0, min(100.0, this_trv_percent))),
        "apply_valve": supports_valve,
        "debug": {
            **asdict(mpc_output.diagnostics),
            "group_valve_pct": group_valve_percent,
            "distributed_valve_pct": this_trv_percent,
            "controller_version": "v2",
            "reid_tau_room": (
                reid_result.tau_room_minutes if reid_result is not None else None
            ),
            "reid_gain": (reid_result.gain_heater if reid_result is not None else None),
        },
    }

    self.schedule_save_state()

    trv_output = replace(
        mpc_output, valve_percent=round(max(0.0, min(100.0, this_trv_percent)))
    )
    return trv_output, supports_valve


def _compute_tpi_balance(
    self: BetterThermostat, entity_id: str
) -> tuple[TpiOutput | None, bool]:
    """Run the TPI balance algorithm for calibration purposes."""

    trv_state = self.real_trvs[entity_id]

    _room_temperature = effective_room_temperature(self)
    if self.heat_target_temperature is None or _room_temperature is None:
        trv_state.calibration_balance = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state.calibration_balance = None
        return None, False

    # Use default TPI params
    params = TpiParams()

    key = build_tpi_key(self, entity_id)
    state_mgr = self.state_mgr
    if state_mgr is None:
        trv_state.calibration_balance = None
        return None, False
    tpi_state = state_mgr.get_tpi(key)
    tpi_state, _tpi_health = sanitize_tpi_state(tpi_state)
    annunciate_health(self, entity_id, _tpi_health)

    try:
        tpi_output, tpi_state = compute_tpi(
            TpiInput(
                key=key,
                room_temperature=_room_temperature,
                target_temperature=self.heat_target_temperature,
                outdoor_temperature=_get_current_outdoor_temperature(self),
                window_open=self.contact_open,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
            ),
            params,
            state=tpi_state,
            now=self.clock.monotonic(),
        )
        state_mgr.set_tpi(key, tpi_state)
    except (ValueError, TypeError, ZeroDivisionError) as err:
        # A healed (sanitized) state must reach the store even when the
        # compute fails, otherwise the poisoned version stays on disk and
        # is re-healed every cycle.
        if _tpi_health != CalibratorHealth.HEALTHY:
            state_mgr.set_tpi(key, tpi_state)
        _LOGGER.debug(
            "better_thermostat %s: TPI calibration compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state.calibration_balance = None
        return None, False

    if tpi_output is None:
        trv_state.calibration_balance = None
        return None, False

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state.calibration_balance = {
        "valve_percent": tpi_output.duty_cycle_percent,
        "apply_valve": supports_valve,
        "debug": tpi_output.debug,
    }

    self.schedule_save_state()

    return tpi_output, supports_valve


def _compute_pid_balance(
    self: BetterThermostat, entity_id: str
) -> tuple[float | None, bool]:
    """Run the PID balance algorithm for calibration purposes."""

    trv_state = self.real_trvs[entity_id]

    _pid_room_temperature = effective_room_temperature(self)
    if self.heat_target_temperature is None or _pid_room_temperature is None:
        trv_state.calibration_balance = None
        return None, False

    state_mgr = self.state_mgr
    if state_mgr is None:
        trv_state.calibration_balance = None
        return None, False

    if self.contact_open is True or self.bt_hvac_mode == HVACMode.OFF:
        # Standby: no actuation, but the measurement chain keeps
        # following the room so control resumes bump-free (the first
        # post-standby cycle sees a fresh measurement and a small dt
        # instead of an hours-old timestamp).
        loop_key = build_pid_loop_key(self, entity_id)
        loop = pid_observe_standby(
            PIDParams(),
            pid_loop_state(
                state_mgr.state.pid, loop_key, build_pid_key(self, entity_id)
            ),
            _pid_room_temperature,
            self.clock.monotonic(),
            inp_room_temperature_filtered=(
                self.room_temperature_filtered
                if _pid_room_temperature is self.room_temperature
                else None
            ),
        )
        state_mgr.set_pid(loop_key, loop)
        trv_state.calibration_balance = None
        return None, False

    # The loop entry carries the integral, the measurement chain and the
    # output across target changes; the bucket entry of the current target
    # carries the gains learned there.
    loop_key = build_pid_loop_key(self, entity_id)
    bucket_key = build_pid_key(self, entity_id)
    loop = pid_loop_state(state_mgr.state.pid, loop_key, bucket_key)
    bucket = state_mgr.state.pid.get(bucket_key)
    if bucket is None:
        bucket = PIDState()

    # Self-heal a poisoned state (non-finite values, runaway gains,
    # wound-up integrator) before it reaches the controller.
    loop, _loop_health = sanitize_pid_state(loop, PIDParams())
    bucket, _bucket_health = sanitize_pid_state(bucket, PIDParams())
    _pid_health = (
        _loop_health if _loop_health != CalibratorHealth.HEALTHY else _bucket_health
    )
    annunciate_health(self, entity_id, _pid_health)

    params = pid_cycle_params(loop, bucket)
    cycle = pid_cycle_state(loop, params, self.heat_target_temperature)

    _LOGGER.debug(
        "better_thermostat %s: Running PID calibration for %s",
        self.device_name,
        entity_id,
    )

    try:
        percent, debug, cycle = compute_pid(
            params,
            self.heat_target_temperature,
            _pid_room_temperature,
            trv_state.current_temperature,
            self.temperature_slope,
            bucket_key,
            inp_room_temperature_filtered=(
                self.room_temperature_filtered
                if _pid_room_temperature is self.room_temperature
                else None
            ),
            max_opening_percent=_get_trv_max_opening(self, entity_id),
            state=cycle,
            now=self.clock.monotonic(),
        )
        state_mgr.set_pid(loop_key, settle_pid_cycle(loop, bucket, cycle, params))
        if params.auto_tune or _bucket_health != CalibratorHealth.HEALTHY:
            state_mgr.set_pid(bucket_key, bucket)
    except (ValueError, TypeError, ZeroDivisionError) as err:
        # A healed (sanitized) state must reach the store even when the
        # compute fails, otherwise the poisoned version stays on disk and
        # is re-healed every cycle.
        if _loop_health != CalibratorHealth.HEALTHY:
            state_mgr.set_pid(loop_key, loop)
        if _bucket_health != CalibratorHealth.HEALTHY:
            state_mgr.set_pid(bucket_key, bucket)
        _LOGGER.debug(
            "better_thermostat %s: PID calibration compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state.calibration_balance = None
        return None, False

    if percent is None:
        trv_state.calibration_balance = None
        return None, False

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state.calibration_balance = {
        "valve_percent": percent,
        "apply_valve": supports_valve,
        "debug": debug,
    }

    _LOGGER.debug(
        "better_thermostat %s: PID calibration for %s: valve_percent=%.1f%%, apply_valve=%s, debug=%s",
        self.device_name,
        entity_id,
        percent,
        supports_valve,
        debug,
    )

    self.schedule_save_state()

    return percent, supports_valve


BALANCE_STRATEGIES = build_strategy_registry(
    _compute_mpc_balance,
    _compute_mpc_v2_balance,
    _compute_tpi_balance,
    _compute_pid_balance,
)


def _aggressive_adjust(
    self: BetterThermostat,
    entity_id: str,
    value: float,
    skip_post: bool,
    ctx: ChannelAdjustment,
) -> tuple[float, bool]:
    """Boost the heating-promoting direction while actively heating.

    While the value lies less than 2.5 past the channel's neutral
    reference in the heating direction, the boost adds a full 2.5 in that
    direction; a value at or beyond that point stays untouched. The result
    therefore jumps at the threshold: 2.4 past neutral becomes 4.9, while
    2.5 past neutral stays 2.5.
    """
    if self.hvac_action == HVACAction.HEATING:
        if ctx.boost_sign * (value - ctx.boost_neutral) < 2.5:
            value += ctx.boost_sign * 2.5
    return value, skip_post


def _heating_power_adjust(
    self: BetterThermostat,
    entity_id: str,
    value: float,
    skip_post: bool,
    ctx: ChannelAdjustment,
) -> tuple[float, bool]:
    """Derive the channel value from the learned heating power."""
    return _heating_power_adjustment(
        self,
        entity_id,
        value,
        hold_value=ctx.hold_value,
        legacy_fallback=ctx.legacy_fallback,
    )


# Any unknown mode runs the plain cascade: no controller, tolerance
# band, post adjustments including the delay.
_PASSIVE_TRAITS = ModeTraits()

MODE_TRAITS: dict[CalibrationMode, ModeTraits] = {
    # Pure offset from external sensor vs TRV temperature; no
    # controller, no tolerance/overheating heuristics.
    CalibrationMode.DEFAULT: ModeTraits(
        needs_target=False, uses_tolerance_band=False, skip_post_adjustments=True
    ),
    CalibrationMode.MPC_CALIBRATION: ModeTraits(
        balance=BALANCE_STRATEGIES[CalibrationMode.MPC_CALIBRATION],
        skip_post_adjustments=True,
    ),
    CalibrationMode.MPC_V2_CALIBRATION: ModeTraits(
        balance=BALANCE_STRATEGIES[CalibrationMode.MPC_V2_CALIBRATION],
        skip_post_adjustments=True,
    ),
    CalibrationMode.TPI_CALIBRATION: ModeTraits(
        balance=BALANCE_STRATEGIES[CalibrationMode.TPI_CALIBRATION],
        skip_post_adjustments=True,
    ),
    CalibrationMode.PID_CALIBRATION: ModeTraits(
        balance=BALANCE_STRATEGIES[CalibrationMode.PID_CALIBRATION],
        skip_post_adjustments=True,
    ),
    # Aggressive starts heating faster: it boosts the channel value and
    # skips the tolerance delay, but keeps overheating protection.
    CalibrationMode.AGGRESSIVE_CALIBRATION: ModeTraits(
        tolerance_delay=False, adjust=_aggressive_adjust
    ),
    # Heating power decides per TRV whether it holds the channel (direct
    # valve control) or derives a value — including the skip flag.
    CalibrationMode.HEATING_POWER_CALIBRATION: ModeTraits(adjust=_heating_power_adjust),
    CalibrationMode.NO_CALIBRATION: _PASSIVE_TRAITS,
}


def _traits_for(mode: CalibrationMode | None) -> ModeTraits:
    """Resolve the traits for a configured calibration mode.

    ``None``, the answer for a mode this version does not know, and a mode
    without an entry of its own fall back to the passive cascade.
    """
    if mode is None:
        return _PASSIVE_TRAITS
    return MODE_TRAITS.get(mode, _PASSIVE_TRAITS)


def _balance_calibrator(
    self: BetterThermostat, entity_id: str, strategy: BalanceStrategy
) -> BalanceCalibrator:
    """Return the TRV's calibrator, rebuilding it when the mode changed.

    The calibrator is the live protocol seam: the dispatch calls
    ``observe`` every cycle and reads the result through
    ``is_ready``/``cached`` — never the strategy directly.
    """
    trv = self.real_trvs[entity_id]
    calibrator = trv.calibrator
    if calibrator is None or calibrator.strategy is not strategy:
        calibrator = BalanceCalibrator(self, entity_id, strategy)
        trv.calibrator = calibrator
    return calibrator


def calculate_calibration_local(self: BetterThermostat, entity_id: str) -> float | None:
    """Calculate local delta to adjust the setpoint of the TRV based on the air temperature of the external sensor.

    This calibration is for devices with local calibration option, it syncs the current temperature of the TRV to the target temperature of
    the external sensor.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the TRV to calibrate

    Returns
    -------
    float
            new local calibration delta
    """
    _context = "_calculate_calibration_local()"

    def _convert_to_float(value: str | int | float | None) -> float | None:
        return convert_to_float(value, self.device_name, _context)

    traits = _traits_for(
        configured_calibration_mode(self.real_trvs[entity_id].advanced)
    )

    _cur_external_temperature = effective_room_temperature(self)
    if _cur_external_temperature is None:
        return None
    if traits.needs_target and self.heat_target_temperature is None:
        return None

    _cur_target_temperature = self.heat_target_temperature

    if traits.uses_tolerance_band and _cur_target_temperature is not None:
        # Add tolerance check – use asymmetric band [target - tol, target]
        # so the TRV stops receiving a heating-promoting calibration once
        # the room reaches the set temperature (not target + tolerance).
        _within_tolerance = (
            _cur_external_temperature >= (_cur_target_temperature - self.tolerance)
            and _cur_external_temperature < _cur_target_temperature
        )

        if _within_tolerance:
            # Within tolerance the calibration holds, but a controller
            # keeps its valve data fresh.
            if traits.balance is not None:
                traits.balance.compute(self, entity_id)
            else:
                self.real_trvs[entity_id].calibration_balance = None
            return self.real_trvs[entity_id].last_calibration

    _cur_trv_temperature_raw = self.real_trvs[entity_id].current_temperature
    _calibration_step = self.real_trvs[entity_id].local_calibration_step
    _calibration_step = _convert_to_float(_calibration_step)
    _cur_trv_temperature = _convert_to_float(_cur_trv_temperature_raw)
    _current_trv_calibration = _convert_to_float(
        self.real_trvs[entity_id].last_calibration
    )

    if (
        _current_trv_calibration is None
        or _cur_external_temperature is None
        or _cur_trv_temperature is None
        or _calibration_step is None
    ):
        _LOGGER.warning(
            "better thermostat %s: %s Could not calculate local calibration in %s: "
            "trv_calibration: %s, trv_temp: %s, external_temp: %s calibration_step: %s",
            self.device_name,
            entity_id,
            _context,
            _current_trv_calibration,
            _cur_trv_temperature,
            _cur_external_temperature,
            _calibration_step,
        )
        return None

    _cur_external_temperature = float(_cur_external_temperature)
    _cur_trv_temperature = float(_cur_trv_temperature)
    _current_trv_calibration = float(_current_trv_calibration)
    _calibration_step = float(_calibration_step)

    # A device that adds the offset to its setpoint regulates on its reading
    # minus the offset and reports the reading alone. Everything below works
    # in the terms of a device that offsets its reading, so the stored offset
    # and the reading are taken into those terms here, and the result goes
    # back into the device's terms once it is final.
    _shifts_setpoint = local_calibration_shifts_setpoint(self, entity_id)
    if _shifts_setpoint:
        _current_trv_calibration = -_current_trv_calibration
        _cur_trv_temperature += _current_trv_calibration

    _new_trv_calibration = (
        _cur_external_temperature - _cur_trv_temperature
    ) + _current_trv_calibration

    if traits.balance is None:
        # DEFAULT and non-controller modes carry no valve/controller data.
        self.real_trvs[entity_id].calibration_balance = None
    else:
        _calibrator = _balance_calibrator(self, entity_id, traits.balance)
        _calibrator.observe(None, self.clock.monotonic())
        _percent, _use_valve = _calibrator.cached()
        if _use_valve:
            _new_trv_calibration = _current_trv_calibration
        elif _percent is not None and _cur_target_temperature is not None:
            _max_temp = _convert_to_float(self.real_trvs[entity_id].max_temp)
            if _max_temp is not None:
                _valve_fraction = max(0.0, min(1.0, _percent / 100.0))
                _desired_trv_setpoint = _cur_trv_temperature + (
                    (float(_max_temp) - _cur_trv_temperature) * _valve_fraction
                )
                if (
                    _valve_fraction == 0.0
                    and _desired_trv_setpoint >= _cur_trv_temperature
                ):
                    _setpoint_drop = _compute_zero_open_offset(
                        self,
                        entity_id,
                        _cur_trv_temperature,
                        _cur_external_temperature,
                        _cur_target_temperature,
                        _calibration_step,
                    )
                    _desired_trv_setpoint = _cur_trv_temperature - _setpoint_drop
                _new_trv_calibration = _current_trv_calibration - (
                    _desired_trv_setpoint - _cur_target_temperature
                )

    _skip_post_adjustments = traits.skip_post_adjustments

    _new_trv_calibration = float(_new_trv_calibration)

    if traits.adjust is not None:

        def _legacy_offset(valve_position: float) -> float:
            return _current_trv_calibration - (
                (self.real_trvs[entity_id].min_local_calibration + _cur_trv_temperature)
                * valve_position
            )

        _new_trv_calibration, _skip_post_adjustments = traits.adjust(
            self,
            entity_id,
            _new_trv_calibration,
            _skip_post_adjustments,
            ChannelAdjustment(
                # Keep the TRV calibration unchanged when the valve is
                # controlled directly.
                hold_value=_current_trv_calibration,
                legacy_fallback=_legacy_offset,
                boost_sign=-1.0,
                boost_neutral=0.0,
            ),
        )

    # Respecting tolerance, delaying heat; modes that should start
    # heating faster opt out of the delay via their traits.
    # The delay is sized by the heating tolerance and carries over unchanged
    # while the cooler runs, where at least as much delay is wanted.
    if not _skip_post_adjustments:
        if traits.tolerance_delay:
            if self.hvac_action in _IDLE_OR_COOLING:
                if _new_trv_calibration < 0.0:
                    _new_trv_calibration += self.tolerance * 2.0

    _new_trv_calibration = fix_local_calibration(self, entity_id, _new_trv_calibration)

    if not _skip_post_adjustments:
        _overheating_protection = advanced_flag(
            self.real_trvs[entity_id].advanced, CONF_PROTECT_OVERHEATING
        )

        # Overheating protection only ever closes the valve: the term counts
        # from heating target + tolerance and is zero below that line.
        if _overheating_protection and _cur_target_temperature is not None:
            if self.hvac_action == HVACAction.IDLE:
                if _cur_external_temperature > _cur_target_temperature + self.tolerance:
                    _new_trv_calibration += (
                        _cur_external_temperature
                        - (_cur_target_temperature + self.tolerance)
                    ) * 8.0

    # Direction-aware rounding for local calibration offset.
    # Calibration offset works inversely to setpoint: a positive offset makes
    # the TRV read a higher temperature (closing the valve), a negative offset
    # makes it read lower (opening the valve).
    # Idle and cooling round the offset UP to ensure the valve closes.
    # When HEATING, round offset DOWN to ensure the valve opens.
    if self.hvac_action in _IDLE_OR_COOLING:
        _cal_rounding = Rounding.up
    elif self.hvac_action == HVACAction.HEATING:
        _cal_rounding = Rounding.down
    else:
        _cal_rounding = Rounding.nearest
    _rounded_calibration = round_by_step(
        _new_trv_calibration, _calibration_step, _cal_rounding
    )
    if _rounded_calibration is None:
        return None
    _new_trv_calibration = _rounded_calibration

    # The device's calibration range is enforced by the safety hull at
    # the command boundary (core/safety.py).
    _new_trv_calibration = _convert_to_float(_new_trv_calibration)
    if _new_trv_calibration is None:
        return None

    # Round to 2 decimals for logging only - the actual calibration value
    # is already rounded by round_by_step based on TRV's calibration_step.
    # Avoid rounding to 1 decimal as this caused precision loss issues
    # (see issues #1792, #1789, #1785).
    _log_calibration: float = round(_new_trv_calibration, 2)
    _log_external_temperature: float = round(_cur_external_temperature, 2)
    _log_trv_temperature: float = round(_cur_trv_temperature, 2)
    _log_current_calibration: float = round(_current_trv_calibration, 2)

    _logmsg = (
        "better_thermostat %s: %s - new local calibration: %s | external_temp: %s, "
        "trv_temp: %s, calibration: %s"
    )

    _LOGGER.debug(
        _logmsg,
        self.device_name,
        entity_id,
        _log_calibration,
        _log_external_temperature,
        _log_trv_temperature,
        _log_current_calibration,
    )

    if _shifts_setpoint:
        return -_new_trv_calibration
    return _new_trv_calibration


def calculate_calibration_setpoint(
    self: BetterThermostat, entity_id: str
) -> float | None:
    """Calculate new setpoint for the TRV based on its own temperature measurement and the air temperature of the external sensor.

    This calibration is for devices with no local calibration option, it syncs the target temperature of the TRV to a new target
    temperature based on the current temperature of the external sensor.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the TRV to calibrate

    Returns
    -------
    float
            new target temperature with calibration
    """
    _context = "_calculate_calibration_setpoint()"

    def _convert_to_float(value: str | int | float | None) -> float | None:
        return convert_to_float(value, self.device_name, _context)

    traits = _traits_for(
        configured_calibration_mode(self.real_trvs[entity_id].advanced)
    )

    # Without a target or a room reading there is no demand, so no valve
    # intent from an earlier cycle may outlive it.
    if self.heat_target_temperature is None:
        self.real_trvs[entity_id].calibration_balance = None
        return None

    _effective_room_temperature = effective_room_temperature(self)
    if _effective_room_temperature is None:
        self.real_trvs[entity_id].calibration_balance = None
        return None
    _cur_external_temperature = float(_effective_room_temperature)
    _cur_target_temperature = float(self.heat_target_temperature)

    _cur_trv_temperature_raw = self.real_trvs[entity_id].current_temperature
    _cur_trv_temperature = _convert_to_float(_cur_trv_temperature_raw)

    # The step is the grid the setpoint is rounded to, so it is kept as the
    # device states it: a 1 °F step on the 0.01 grid of a reading, 0.56 K,
    # drifts off whole degrees Fahrenheit within a few steps. A TRV published
    # in whole degrees Fahrenheit is written on them, so the rounding toward
    # the heating direction lands on the grid the write goes out on.
    _trv_temperature_step = published_setpoint_grid(
        normalize_step(self.real_trvs[entity_id].target_temp_step),
        self.hass.states.get(entity_id),
        self.hass.config.units.temperature_unit,
    )

    # The controllers size the valve from the room, not from the TRV's own
    # reading, so the valve intent is refreshed before the setpoint, which
    # needs that reading, can give up for want of it.
    _percent: float | None = None
    _use_valve = False
    if traits.balance is None:
        # DEFAULT and non-controller modes carry no valve/controller data.
        self.real_trvs[entity_id].calibration_balance = None
    else:
        _calibrator = _balance_calibrator(self, entity_id, traits.balance)
        _calibrator.observe(None, self.clock.monotonic())
        _percent, _use_valve = _calibrator.cached()

    if _cur_trv_temperature is None:
        if traits.adjust is not None:
            # Only the valve intent the adjustment publishes is wanted; the
            # setpoint it derives is not sent without the TRV's reading.
            traits.adjust(
                self,
                entity_id,
                _cur_target_temperature,
                traits.skip_post_adjustments,
                ChannelAdjustment(
                    hold_value=_cur_target_temperature,
                    legacy_fallback=lambda _valve_position: _cur_target_temperature,
                    boost_sign=1.0,
                    boost_neutral=_cur_target_temperature,
                ),
            )
        return None

    _cur_trv_temperature = float(_cur_trv_temperature)

    _calibrated_setpoint = (
        _cur_target_temperature - _cur_external_temperature
    ) + _cur_trv_temperature

    if traits.balance is not None:
        if _use_valve and _percent is not None:
            if float(_percent) == 0.0:
                # Valve closed: push setpoint below TRV's own temperature so it doesn't
                # heat by itself even though direct valve control already sent 0%.
                _setpoint_drop = _compute_zero_open_offset(
                    self,
                    entity_id,
                    _cur_trv_temperature,
                    _cur_external_temperature,
                    _cur_target_temperature,
                    _trv_temperature_step,
                )
                _calibrated_setpoint = _cur_trv_temperature - _setpoint_drop
            else:
                # Valve open: keep target so TRV internal logic doesn't restrict us.
                _calibrated_setpoint = _cur_target_temperature
        elif not _use_valve and _percent is not None:
            _max_temp = _convert_to_float(self.real_trvs[entity_id].max_temp)
            if _max_temp is not None:
                _valve_fraction = max(0.0, min(1.0, float(_percent) / 100.0))
                _calibrated_setpoint = _cur_trv_temperature + (
                    (float(_max_temp) - _cur_trv_temperature) * _valve_fraction
                )
                if (
                    _valve_fraction == 0.0
                    and _calibrated_setpoint >= _cur_trv_temperature
                ):
                    _setpoint_drop = _compute_zero_open_offset(
                        self,
                        entity_id,
                        _cur_trv_temperature,
                        _cur_external_temperature,
                        _cur_target_temperature,
                        _trv_temperature_step,
                    )
                    _calibrated_setpoint = _cur_trv_temperature - _setpoint_drop

    _skip_post_adjustments = traits.skip_post_adjustments

    if traits.adjust is not None:

        def _legacy_setpoint(valve_position: float) -> float:
            max_temp = _convert_to_float(self.real_trvs[entity_id].max_temp)
            if max_temp is None:
                return _calibrated_setpoint
            return _cur_trv_temperature + (
                (float(max_temp) - _cur_trv_temperature) * valve_position
            )

        _calibrated_setpoint, _skip_post_adjustments = traits.adjust(
            self,
            entity_id,
            _calibrated_setpoint,
            _skip_post_adjustments,
            ChannelAdjustment(
                # Keep the TRV at the BT target when the valve is
                # controlled directly.
                hold_value=_cur_target_temperature,
                legacy_fallback=_legacy_setpoint,
                boost_sign=1.0,
                boost_neutral=_cur_trv_temperature,
            ),
        )

    _calibrated_setpoint = float(_calibrated_setpoint)

    # Respecting tolerance, delaying heat; modes that should start
    # heating faster opt out of the delay via their traits.
    # The delay is sized by the heating tolerance and carries over unchanged
    # while the cooler runs, where at least as much delay is wanted.
    if not _skip_post_adjustments:
        if traits.tolerance_delay:
            if self.hvac_action in _IDLE_OR_COOLING:
                if _calibrated_setpoint - _cur_trv_temperature > 0.0:
                    _calibrated_setpoint -= self.tolerance * 2.0

    _calibrated_setpoint = fix_target_temperature_calibration(
        self, entity_id, _calibrated_setpoint
    )

    if not _skip_post_adjustments:
        _overheating_protection = advanced_flag(
            self.real_trvs[entity_id].advanced, CONF_PROTECT_OVERHEATING
        )

        # Overheating protection only ever closes the valve: the term counts
        # from heating target + tolerance and is zero below that line.
        if _overheating_protection:
            if self.hvac_action == HVACAction.IDLE:
                if _cur_external_temperature > _cur_target_temperature + self.tolerance:
                    _calibrated_setpoint -= (
                        _cur_external_temperature
                        - (_cur_target_temperature + self.tolerance)
                    ) * 8.0

    # Direction-aware rounding: idle and cooling round the setpoint DOWN so the
    # TRV sees a target below its current temperature and closes the valve.
    # When HEATING, round UP so the TRV keeps the valve open.
    # This prevents integer-step TRVs (step=1.0) from rounding a value like
    # 19.7 up to 20.0 which would keep the valve open at the current temperature.
    if self.hvac_action in _IDLE_OR_COOLING:
        _step_rounding = Rounding.down
    elif self.hvac_action == HVACAction.HEATING:
        _step_rounding = Rounding.up
    else:
        _step_rounding = Rounding.nearest
    _rounded_setpoint = round_by_step(
        _calibrated_setpoint, _trv_temperature_step, _step_rounding
    )
    if _rounded_setpoint is None:
        return None
    _calibrated_setpoint = _rounded_setpoint

    # The TRV's min/max range is enforced by the safety hull at the
    # command boundary (core/safety.py).

    _logmsg = (
        "better_thermostat %s: %s - new setpoint calibration: %s | external_temp: %s, "
        "target_temp: %s, trv_temp: %s"
    )

    _LOGGER.debug(
        _logmsg,
        self.device_name,
        entity_id,
        _calibrated_setpoint,
        _cur_external_temperature,
        _cur_target_temperature,
        _cur_trv_temperature,
    )

    return _calibrated_setpoint
