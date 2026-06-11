"""Helper functions for the Better Thermostat component."""

import logging
import math

from homeassistant.components.climate.const import HVACAction, HVACMode

from custom_components.better_thermostat.core.calibrator import CalibratorHealth
from custom_components.better_thermostat.core.fsm.control_mode import ControlMode
from custom_components.better_thermostat.model_fixes.model_quirks import (
    fix_local_calibration,
    fix_target_temperature_calibration,
)
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    build_mpc_group_key,
    build_mpc_key,
    compute_mpc,
    distribute_valve_percent,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    DEFAULT_PID_AUTO_TUNE,
    DEFAULT_PID_KD,
    DEFAULT_PID_KI,
    DEFAULT_PID_KP,
    PIDParams,
    build_pid_key,
    compute_pid,
    sanitize_pid_state,
)
from custom_components.better_thermostat.utils.calibration.strategies import (
    ChannelAdjustment,
    ModeTraits,
    build_strategy_registry,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    build_tpi_key,
    compute_tpi,
)
from custom_components.better_thermostat.utils.const import (
    CONF_PROTECT_OVERHEATING,
    CalibrationMode,
    CalibrationType,
)
from custom_components.better_thermostat.utils.helpers import (
    clamp_valve_percent,
    convert_to_float,
    convert_to_float_celsius,
    heating_power_valve_position,
    normalize_calibration_mode,
    round_by_step,
    rounding,
)

_LOGGER = logging.getLogger(__name__)


def _compute_zero_open_offset(
    self,
    entity_id: str,
    _cur_trv_temp: float,
    _cur_external_temp: float,
    _cur_target_temp: float,
    _trv_temp_step: float,
) -> float:
    """Compute the offset to push setpoint below TRV temp when valve fraction is zero.

    Returns the offset so that callers can set:
        _calibrated_setpoint = _cur_trv_temp - offset
    """
    _overshoot = max(0.0, _cur_external_temp - _cur_target_temp)
    _t_min = convert_to_float(
        str(self.real_trvs[entity_id].min_temp),
        self.device_name,
        "_compute_zero_open_offset()",
    )
    _max_offset = max(1.0, _cur_trv_temp - float(_t_min)) if _t_min is not None else 8.0
    _offset = _max_offset * (1.0 - math.exp(-0.5 * _overshoot))
    _offset = max(_trv_temp_step, _offset)
    return _offset


def effective_room_temp(self) -> float | None:
    """Room temperature for the control law, honoring the fail-soft ladder.

    Under SENSOR_FALLBACK the mean of the available TRV-internal
    temperatures substitutes the (dead) room sensor — completing the
    fallback that the watcher has always announced. On every other rung
    this is simply the current room temperature.
    """
    mode = self.kernel_state.control_mode.mode
    if mode == ControlMode.SENSOR_FALLBACK:
        temps = []
        for trv in self.real_trvs.values():
            value = trv.current_temperature
            if isinstance(value, (int, float)):
                temps.append(float(value))
        if temps:
            return sum(temps) / len(temps)
    return self.cur_temp


def _get_current_outdoor_temp(self) -> float | None:
    """Get current outdoor temperature from outdoor sensor or weather entity."""
    if self.outdoor_sensor is not None:
        state = self.hass.states.get(self.outdoor_sensor)
        if state:
            return convert_to_float_celsius(
                state.state,
                self.device_name,
                "_get_current_outdoor_temp()",
                unit_of_measurement=state.attributes.get("unit_of_measurement"),
            )

    if self.weather_entity is not None:
        state = self.hass.states.get(self.weather_entity)
        if state and state.attributes:
            return convert_to_float_celsius(
                state.attributes.get("temperature"),
                self.device_name,
                "_get_current_outdoor_temp()",
                unit_of_measurement=state.attributes.get("temperature_unit"),
            )

    return None


def _get_solar_context(self) -> tuple[bool, float]:
    """Daylight flag plus current solar intensity (0.0 below the horizon)."""
    is_day = True
    if self.hass is not None:
        sun = self.hass.states.get("sun.sun")
        if sun is not None and sun.state == "below_horizon":
            is_day = False
    return is_day, (_get_current_solar_intensity(self) if is_day else 0.0)


def _get_current_solar_intensity(self) -> float:
    """Estimate solar intensity (0.0 to 1.0) based on weather entity data."""
    if self.weather_entity is None:
        return 0.0

    state = self.hass.states.get(self.weather_entity)
    if not state or not state.attributes:
        return 0.0

    def _get_val(data, key):
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
            except (ValueError, TypeError):
                pass

    # 2. UV Index (0-10+) -> Higher is better
    for source in sources:
        uv = _get_val(source, "uv_index")
        if uv is not None:
            try:
                # Normalize UV index (approx 0-10 range)
                return max(0.0, min(1.0, float(uv) / 10.0))
            except (ValueError, TypeError):
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


def _supports_direct_valve_control(self, entity_id: str) -> bool:
    """Return True if the TRV supports writing a valve percentage."""

    _calibration_type = self.real_trvs[entity_id].advanced.get(
        "calibration", CalibrationType.TARGET_TEMP_BASED
    )
    if _calibration_type != CalibrationType.DIRECT_VALVE_BASED:
        return False

    trv_data = self.real_trvs.get(entity_id)
    if trv_data is None:
        return False
    return trv_data.capabilities().supports_valve_write


def _get_trv_max_opening(self, entity_id: str) -> float | None:
    """Return the user-defined max opening percent for a TRV, if any."""

    trv_state = self.real_trvs.get(entity_id)
    max_opening = trv_state.valve_max_opening if trv_state is not None else None
    if isinstance(max_opening, (int, float)):
        return max(0.0, min(100.0, float(max_opening)))
    return None


def _heating_power_adjustment(
    self, entity_id: str, current_value: float, *, hold_value: float, legacy_fallback
) -> tuple[float, bool]:
    """Shared HEATING_POWER machinery for both calibration channels.

    When direct valve control is available, publish the valve intent
    (closed while not heating, the heating-power position otherwise) and
    hold the channel value so the calibration does not counteract the
    valve command. Without valve support, fall back to the channel's
    legacy valve-position math.

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

    _valve_position = heating_power_valve_position(self, entity_id)
    if _supports_direct_valve_control(self, entity_id) and isinstance(
        _valve_position, (int, float)
    ):
        try:
            _pct = clamp_valve_percent(float(_valve_position) * 100.0)
        except (TypeError, ValueError):
            _pct = None
        if _pct is not None:
            trv.calibration_balance = {
                "valve_percent": _pct,
                "apply_valve": True,
                "debug": {"source": "heating_power_calibration"},
            }
            return hold_value, True
        return legacy_fallback(_valve_position), False

    trv.calibration_balance = None
    return legacy_fallback(_valve_position), False


def _compute_mpc_balance(self, entity_id: str):
    """Run the MPC balance algorithm for calibration purposes.

    When the BT instance controls **multiple TRVs**, a single shared MPC model
    is evaluated once (using the room-level external sensor) and the resulting
    valve command is distributed across TRVs proportional to their internal
    temperature deficit.  A cold TRV (low ``current_temperature``) receives
    *more* valve opening; a warm one receives *less*.

    For a **single TRV** this behaves exactly as before (no distribution step).
    """

    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return None, False

    if self.bt_target_temp is None or self.cur_temp is None:
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
        trv_temps = {}
        warmest_temp: float | None = None
        for eid, tdata in self.real_trvs.items():
            _t = tdata.current_temperature
            if _t is not None:
                try:
                    temp_val = float(_t)
                    trv_temps[eid] = temp_val
                    if warmest_temp is None or temp_val > warmest_temp:
                        warmest_temp = temp_val
                        warmest_trv_id = eid
                except (TypeError, ValueError):
                    trv_temps[eid] = None
            else:
                trv_temps[eid] = None

    max_opening_pct = _get_trv_max_opening(
        self, warmest_trv_id if is_multi_trv else entity_id
    )

    params = MpcParams()

    # Optional: use filtered external temperature for MPC cost evaluation to reduce jitter.
    # `cur_temp_filtered` is maintained by events/temperature.py (EMA) and passed separately.
    mpc_current_temp = effective_room_temp(self)
    mpc_filtered_temp = (
        self.cur_temp_filtered if mpc_current_temp is self.cur_temp else None
    )

    _is_day, _solar_intensity = _get_solar_context(self)

    # Use a group key for multi-TRV setups so all TRVs share one MPC model.
    if is_multi_trv:
        mpc_key = build_mpc_group_key(self)
    else:
        mpc_key = build_mpc_key(self, entity_id)

    mpc_state = self.state_mgr.get_mpc(mpc_key)

    try:
        mpc_output, mpc_state = compute_mpc(
            MpcInput(
                key=mpc_key,
                target_temp_C=self.bt_target_temp,
                current_temp_C=mpc_current_temp,
                filtered_temp_C=mpc_filtered_temp,
                trv_temp_C=trv_state.current_temperature,
                tolerance_K=float(self.tolerance or 0.0),
                temp_slope_K_per_min=self.temp_slope,
                window_open=self.window_open or False,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
                outdoor_temp_C=_get_current_outdoor_temp(self),
                is_day=_is_day,
                solar_intensity=_solar_intensity,
                max_opening_pct=max_opening_pct,
            ),
            params,
            state=mpc_state,
            all_states=self.state_mgr.state.mpc,
        )
        self.state_mgr.set_mpc(mpc_key, mpc_state)
    except (ValueError, TypeError, ZeroDivisionError) as err:
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

    group_valve_pct = float(mpc_output.valve_percent)

    # --- Multi-TRV distribution ---
    if is_multi_trv:
        trv_temps = trv_temps or {}
        distributed = distribute_valve_percent(
            u_total_pct=group_valve_pct, trv_temps=trv_temps
        )
        this_trv_pct = distributed.get(entity_id, group_valve_pct)

        _LOGGER.debug(
            "better_thermostat %s: MPC grouped distribution for %s: "
            "group_pct=%.1f%% → this_trv_pct=%.1f%% | trv_temps=%s → distributed=%s",
            self.device_name,
            entity_id,
            group_valve_pct,
            this_trv_pct,
            {k: round(v, 1) if v is not None else None for k, v in trv_temps.items()},
            {k: round(v, 1) for k, v in distributed.items()},
        )
    else:
        this_trv_pct = group_valve_pct

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state.calibration_balance = {
        "valve_percent": clamp_valve_percent(this_trv_pct),
        "apply_valve": supports_valve,
        "debug": {
            **(getattr(mpc_output, "debug", None) or {}),
            "group_valve_pct": group_valve_pct,
            "distributed_valve_pct": this_trv_pct,
        },
    }

    _schedule_mpc = getattr(self, "schedule_save_state", None)
    if callable(_schedule_mpc):
        _schedule_mpc()

    # Return an MpcOutput-like object with the TRV-specific valve_percent
    from dataclasses import replace as _dc_replace

    trv_output = _dc_replace(
        mpc_output, valve_percent=clamp_valve_percent(this_trv_pct)
    )

    return trv_output, supports_valve


def _compute_tpi_balance(self, entity_id: str):
    """Run the TPI balance algorithm for calibration purposes."""

    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return None, False

    if self.bt_target_temp is None or self.cur_temp is None:
        trv_state.calibration_balance = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state.calibration_balance = None
        return None, False

    # Use default TPI params
    params = TpiParams()

    key = build_tpi_key(self, entity_id)
    tpi_state = self.state_mgr.get_tpi(key)

    try:
        tpi_output, tpi_state = compute_tpi(
            TpiInput(
                key=key,
                current_temp_C=effective_room_temp(self),
                target_temp_C=self.bt_target_temp,
                outdoor_temp_C=_get_current_outdoor_temp(self),
                window_open=self.window_open or False,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
            ),
            params,
            state=tpi_state,
        )
        self.state_mgr.set_tpi(key, tpi_state)
    except (ValueError, TypeError, ZeroDivisionError) as err:
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
        "valve_percent": tpi_output.duty_cycle_pct,
        "apply_valve": supports_valve,
        "debug": getattr(tpi_output, "debug", None),
    }

    if callable(getattr(self, "schedule_save_state", None)):
        self.schedule_save_state()

    return tpi_output, supports_valve


def _compute_pid_balance(self, entity_id: str):
    """Run the PID balance algorithm for calibration purposes."""

    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return None, False

    if self.bt_target_temp is None or self.cur_temp is None:
        trv_state.calibration_balance = None
        return None, False

    if self.window_open is True:
        trv_state.calibration_balance = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state.calibration_balance = None
        return None, False

    # Build PID params from config and learned values
    key = build_pid_key(self, entity_id)
    pid_state = self.state_mgr.get_pid(key)

    # Self-heal a poisoned state (non-finite values, runaway gains,
    # wound-up integrator) before it reaches the controller.
    pid_state, _pid_health = sanitize_pid_state(pid_state, PIDParams())
    if _pid_health != CalibratorHealth.HEALTHY:
        _LOGGER.warning(
            "better_thermostat %s: PID state for %s self-healed (%s)",
            self.device_name,
            entity_id,
            _pid_health,
        )

    # Use learned gains if available, otherwise from config, otherwise defaults
    params = PIDParams(
        kp=(
            pid_state.pid_kp
            if pid_state and pid_state.pid_kp is not None
            else DEFAULT_PID_KP
        ),
        ki=(
            pid_state.pid_ki
            if pid_state and pid_state.pid_ki is not None
            else DEFAULT_PID_KI
        ),
        kd=(
            pid_state.pid_kd
            if pid_state and pid_state.pid_kd is not None
            else DEFAULT_PID_KD
        ),
        auto_tune=(
            pid_state.auto_tune
            if pid_state and pid_state.auto_tune is not None
            else DEFAULT_PID_AUTO_TUNE
        ),
    )

    _pid_room_temp = effective_room_temp(self)

    _LOGGER.debug(
        "better_thermostat %s: Running PID calibration for %s",
        self.device_name,
        entity_id,
    )

    try:
        percent, debug, pid_state = compute_pid(
            params,
            self.bt_target_temp,
            _pid_room_temp,
            trv_state.current_temperature,
            self.temp_slope,
            key,
            inp_current_temp_ema_C=(
                self.cur_temp_filtered if _pid_room_temp is self.cur_temp else None
            ),
            max_opening_pct=_get_trv_max_opening(self, entity_id),
            state=pid_state,
        )
        self.state_mgr.set_pid(key, pid_state)
    except (ValueError, TypeError, ZeroDivisionError) as err:
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
        getattr(self, "device_name", "unknown"),
        entity_id,
        percent,
        supports_valve,
        debug,
    )

    return percent, supports_valve


BALANCE_STRATEGIES = build_strategy_registry(
    _compute_mpc_balance, _compute_tpi_balance, _compute_pid_balance
)


def _aggressive_adjust(self, entity_id, value, skip_post, ctx):
    """Boost the heating-promoting direction while actively heating.

    The boost only tops the value up to 2.5 past the channel's neutral
    reference; a value already past that point stays untouched.
    """
    if self.hvac_action == HVACAction.HEATING:
        if ctx.boost_sign * (value - ctx.boost_neutral) < 2.5:
            value += ctx.boost_sign * 2.5
    return value, skip_post


def _heating_power_adjust(self, entity_id, value, skip_post, ctx):
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
        needs_target=False,
        uses_tolerance_band=False,
        skip_post_adjustments=True,
    ),
    CalibrationMode.MPC_CALIBRATION: ModeTraits(
        balance=BALANCE_STRATEGIES[CalibrationMode.MPC_CALIBRATION],
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
    CalibrationMode.AGGRESIVE_CALIBRATION: ModeTraits(
        tolerance_delay=False,
        adjust=_aggressive_adjust,
    ),
    # Heating power decides per TRV whether it holds the channel (direct
    # valve control) or derives a value — including the skip flag.
    CalibrationMode.HEATING_POWER_CALIBRATION: ModeTraits(
        adjust=_heating_power_adjust,
    ),
    CalibrationMode.NO_CALIBRATION: _PASSIVE_TRAITS,
}


def calculate_calibration_local(self, entity_id) -> float | None:
    """Calculate local delta to adjust the setpoint of the TRV based on the air temperature of the external sensor.

    This calibration is for devices with local calibration option, it syncs the current temperature of the TRV to the target temperature of
    the external sensor.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    float
            new local calibration delta
    """
    _context = "_calculate_calibration_local()"

    def _convert_to_float(value):
        return convert_to_float(value, self.name, _context)

    _calibration_mode = normalize_calibration_mode(
        self.real_trvs[entity_id].advanced.get(
            "calibration_mode", CalibrationMode.MPC_CALIBRATION
        )
    )
    if _calibration_mode is None:
        _calibration_mode = CalibrationMode.MPC_CALIBRATION
    traits = MODE_TRAITS.get(_calibration_mode, _PASSIVE_TRAITS)

    if self.cur_temp is None:
        return None
    if traits.needs_target and self.bt_target_temp is None:
        return None

    _cur_external_temp = effective_room_temp(self)
    _cur_target_temp = self.bt_target_temp

    if traits.uses_tolerance_band:
        # Add tolerance check – use asymmetric band [target - tol, target]
        # so the TRV stops receiving a heating-promoting calibration once
        # the room reaches the set temperature (not target + tolerance).
        _within_tolerance = (
            _cur_external_temp >= (_cur_target_temp - self.tolerance)
            and _cur_external_temp < _cur_target_temp
        )

        if _within_tolerance:
            # Within tolerance the calibration holds, but a controller
            # keeps its valve data fresh.
            if traits.balance is not None:
                traits.balance.compute(self, entity_id)
            else:
                self.real_trvs[entity_id].calibration_balance = None
            return self.real_trvs[entity_id].last_calibration

    _cur_trv_temp_s = self.real_trvs[entity_id].current_temperature
    _calibration_step = self.real_trvs[entity_id].local_calibration_step
    _calibration_step = _convert_to_float(_calibration_step)
    _cur_trv_temp_f = _convert_to_float(_cur_trv_temp_s)
    _current_trv_calibration = _convert_to_float(
        self.real_trvs[entity_id].last_calibration
    )

    if (
        _current_trv_calibration is None
        or _cur_external_temp is None
        or _cur_trv_temp_f is None
        or _calibration_step is None
    ):
        _LOGGER.warning(
            "better thermostat %s: %s Could not calculate local calibration in %s: "
            "trv_calibration: %s, trv_temp: %s, external_temp: %s calibration_step: %s",
            self.device_name,
            entity_id,
            _context,
            _current_trv_calibration,
            _cur_trv_temp_f,
            _cur_external_temp,
            _calibration_step,
        )
        return None

    _cur_external_temp = float(_cur_external_temp)
    if traits.needs_target:
        _cur_target_temp = float(_cur_target_temp)
    _cur_trv_temp_f = float(_cur_trv_temp_f)
    _current_trv_calibration = float(_current_trv_calibration)
    _calibration_step = float(_calibration_step)

    _new_trv_calibration = (
        _cur_external_temp - _cur_trv_temp_f
    ) + _current_trv_calibration

    if traits.balance is None:
        # DEFAULT and non-controller modes carry no valve/controller data.
        self.real_trvs[entity_id].calibration_balance = None
    else:
        _percent, _use_valve = traits.balance.run(self, entity_id)
        if _use_valve:
            _new_trv_calibration = _current_trv_calibration
        elif _percent is not None:
            _max_temp = _convert_to_float(self.real_trvs[entity_id].max_temp)
            if _max_temp is not None:
                _valve_fraction = max(0.0, min(1.0, _percent / 100.0))
                _desired_trv_setpoint = _cur_trv_temp_f + (
                    (float(_max_temp) - _cur_trv_temp_f) * _valve_fraction
                )
                if _valve_fraction == 0.0 and _desired_trv_setpoint >= _cur_trv_temp_f:
                    _offset = _compute_zero_open_offset(
                        self,
                        entity_id,
                        _cur_trv_temp_f,
                        _cur_external_temp,
                        _cur_target_temp,
                        _calibration_step,
                    )
                    _desired_trv_setpoint = _cur_trv_temp_f - _offset
                _new_trv_calibration = _current_trv_calibration - (
                    _desired_trv_setpoint - _cur_target_temp
                )

    if _new_trv_calibration is None:
        return None

    _skip_post_adjustments = traits.skip_post_adjustments

    _new_trv_calibration = float(_new_trv_calibration)

    if traits.adjust is not None:

        def _legacy_offset(valve_position):
            return _current_trv_calibration - (
                (self.real_trvs[entity_id].local_calibration_min + _cur_trv_temp_f)
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
    if not _skip_post_adjustments:
        if traits.tolerance_delay:
            if self.hvac_action == HVACAction.IDLE:
                if _new_trv_calibration < 0.0:
                    _new_trv_calibration += self.tolerance * 2.0

    _new_trv_calibration = fix_local_calibration(self, entity_id, _new_trv_calibration)

    if not _skip_post_adjustments:
        _overheating_protection = self.real_trvs[entity_id].advanced.get(
            CONF_PROTECT_OVERHEATING, False
        )

        # Additional adjustment if overheating protection is enabled
        if _overheating_protection is True:
            if self.hvac_action == HVACAction.IDLE:
                _new_trv_calibration += (
                    _cur_external_temp - (_cur_target_temp + self.tolerance)
                ) * 8.0  # Reduced from 10.0 since we already add 2.0

    # Direction-aware rounding for local calibration offset.
    # Calibration offset works inversely to setpoint: a positive offset makes
    # the TRV read a higher temperature (closing the valve), a negative offset
    # makes it read lower (opening the valve).
    # When IDLE, round offset UP to ensure the valve closes.
    # When HEATING, round offset DOWN to ensure the valve opens.
    if self.hvac_action == HVACAction.IDLE:
        _cal_rounding = rounding.up
    elif self.hvac_action == HVACAction.HEATING:
        _cal_rounding = rounding.down
    else:
        _cal_rounding = rounding.nearest
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
    _log_external_temp: float = round(_cur_external_temp, 2)
    _log_trv_temp: float = round(_cur_trv_temp_f, 2)
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
        _log_external_temp,
        _log_trv_temp,
        _log_current_calibration,
    )

    return _new_trv_calibration


def calculate_calibration_setpoint(self, entity_id) -> float | None:
    """Calculate new setpoint for the TRV based on its own temperature measurement and the air temperature of the external sensor.

    This calibration is for devices with no local calibration option, it syncs the target temperature of the TRV to a new target
    temperature based on the current temperature of the external sensor.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    float
            new target temp with calibration
    """
    _context = "_calculate_calibration_setpoint()"

    def _convert_to_float(value):
        return convert_to_float(value, self.name, _context)

    _calibration_mode = normalize_calibration_mode(
        self.real_trvs[entity_id].advanced.get(
            "calibration_mode", CalibrationMode.MPC_CALIBRATION
        )
    )
    if _calibration_mode is None:
        _calibration_mode = CalibrationMode.MPC_CALIBRATION
    traits = MODE_TRAITS.get(_calibration_mode, _PASSIVE_TRAITS)

    if self.cur_temp is None or self.bt_target_temp is None:
        return None

    # cur_temp is not None here, so effective_room_temp() never is —
    # and a fallback reading of exactly 0.0 is a reading, not a gap.
    _cur_external_temp = float(effective_room_temp(self))
    _cur_target_temp = float(self.bt_target_temp)

    _cur_trv_temp_s = self.real_trvs[entity_id].current_temperature
    _cur_trv_temp = _convert_to_float(_cur_trv_temp_s)

    _trv_temp_step_raw = self.real_trvs[entity_id].target_temp_step
    _trv_temp_step = _convert_to_float(_trv_temp_step_raw)
    if _trv_temp_step is None or _trv_temp_step <= 0:
        _trv_temp_step = 0.5

    if _cur_trv_temp is None:
        return None

    _cur_trv_temp = float(_cur_trv_temp)

    _calibrated_setpoint = (_cur_target_temp - _cur_external_temp) + _cur_trv_temp

    if traits.balance is None:
        # DEFAULT and non-controller modes carry no valve/controller data.
        self.real_trvs[entity_id].calibration_balance = None
    else:
        _percent, _use_valve = traits.balance.run(self, entity_id)
        if _use_valve and _percent is not None:
            if float(_percent) == 0.0:
                # Valve closed: push setpoint below TRV's own temp so it doesn't
                # heat by itself even though direct valve control already sent 0%.
                _offset = _compute_zero_open_offset(
                    self,
                    entity_id,
                    _cur_trv_temp,
                    _cur_external_temp,
                    _cur_target_temp,
                    _trv_temp_step,
                )
                _calibrated_setpoint = _cur_trv_temp - _offset
            else:
                # Valve open: keep target so TRV internal logic doesn't restrict us.
                _calibrated_setpoint = _cur_target_temp
        elif not _use_valve and _percent is not None:
            _max_temp = _convert_to_float(self.real_trvs[entity_id].max_temp)
            if _max_temp is not None:
                _valve_fraction = max(0.0, min(1.0, float(_percent) / 100.0))
                _calibrated_setpoint = _cur_trv_temp + (
                    (float(_max_temp) - _cur_trv_temp) * _valve_fraction
                )
                if _valve_fraction == 0.0 and _calibrated_setpoint >= _cur_trv_temp:
                    _offset = _compute_zero_open_offset(
                        self,
                        entity_id,
                        _cur_trv_temp,
                        _cur_external_temp,
                        _cur_target_temp,
                        _trv_temp_step,
                    )
                    _calibrated_setpoint = _cur_trv_temp - _offset

    _skip_post_adjustments = traits.skip_post_adjustments

    if traits.adjust is not None:

        def _legacy_setpoint(valve_position):
            max_temp = _convert_to_float(self.real_trvs[entity_id].max_temp)
            if max_temp is None:
                return _calibrated_setpoint
            return _cur_trv_temp + ((float(max_temp) - _cur_trv_temp) * valve_position)

        _calibrated_setpoint, _skip_post_adjustments = traits.adjust(
            self,
            entity_id,
            _calibrated_setpoint,
            _skip_post_adjustments,
            ChannelAdjustment(
                # Keep the TRV at the BT target when the valve is
                # controlled directly.
                hold_value=_cur_target_temp,
                legacy_fallback=_legacy_setpoint,
                boost_sign=1.0,
                boost_neutral=_cur_trv_temp,
            ),
        )

    if _calibrated_setpoint is None:
        return None

    _calibrated_setpoint = float(_calibrated_setpoint)

    # Respecting tolerance, delaying heat; modes that should start
    # heating faster opt out of the delay via their traits.
    if not _skip_post_adjustments:
        if traits.tolerance_delay:
            if self.hvac_action == HVACAction.IDLE:
                if _calibrated_setpoint - _cur_trv_temp > 0.0:
                    _calibrated_setpoint -= self.tolerance * 2.0

    _calibrated_setpoint = fix_target_temperature_calibration(
        self, entity_id, _calibrated_setpoint
    )

    if not _skip_post_adjustments:
        _overheating_protection = self.real_trvs[entity_id].advanced.get(
            CONF_PROTECT_OVERHEATING, False
        )

        # Additional adjustment if overheating protection is enabled
        if _overheating_protection is True:
            if self.hvac_action == HVACAction.IDLE:
                _calibrated_setpoint -= (
                    _cur_external_temp - (_cur_target_temp + self.tolerance)
                ) * 8.0  # Reduced from 10.0 since we already subtract 2.0

    # Direction-aware rounding: when IDLE, round setpoint DOWN so the TRV
    # sees a target below its current temperature and closes the valve.
    # When HEATING, round UP so the TRV keeps the valve open.
    # This prevents integer-step TRVs (step=1.0) from rounding a value like
    # 19.7 up to 20.0 which would keep the valve open at the current temp.
    if self.hvac_action == HVACAction.IDLE:
        _step_rounding = rounding.down
    elif self.hvac_action == HVACAction.HEATING:
        _step_rounding = rounding.up
    else:
        _step_rounding = rounding.nearest
    _rounded_setpoint = round_by_step(
        _calibrated_setpoint, _trv_temp_step, _step_rounding
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
        _cur_external_temp,
        _cur_target_temp,
        _cur_trv_temp,
    )

    return _calibrated_setpoint
