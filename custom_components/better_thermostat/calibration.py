"""Helper functions for the Better Thermostat component."""

import logging

from homeassistant.components.climate.const import HVACAction, HVACMode

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CalibrationType,
    CONF_PROTECT_OVERHEATING,
)

from custom_components.better_thermostat.utils.helpers import (
    convert_to_float,
    round_by_step,
    heating_power_valve_position,
)

from custom_components.better_thermostat.model_fixes.model_quirks import (
    fix_local_calibration,
    fix_target_temperature_calibration,
)

from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    build_mpc_key,
    compute_mpc,
)

from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    build_tpi_key,
    compute_tpi,
)

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    compute_pid,
    get_pid_state,
    DEFAULT_PID_KP,
    DEFAULT_PID_KI,
    DEFAULT_PID_KD,
    DEFAULT_PID_AUTO_TUNE,
)

from custom_components.better_thermostat.utils.calibration.pid import build_pid_key

_LOGGER = logging.getLogger(__name__)


def _get_current_outdoor_temp(self) -> float | None:
    """Get current outdoor temperature from outdoor sensor or weather entity."""
    if self.outdoor_sensor is not None:
        state = self.hass.states.get(self.outdoor_sensor)
        if state:
            return convert_to_float(
                state.state, self.device_name, "_get_current_outdoor_temp()"
            )

    if self.weather_entity is not None:
        state = self.hass.states.get(self.weather_entity)
        if state and state.attributes:
            return convert_to_float(
                state.attributes.get("temperature"),
                self.device_name,
                "_get_current_outdoor_temp()",
            )

    return None


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

    _calibration_type = self.real_trvs[entity_id]["advanced"].get(
        "calibration", CalibrationType.TARGET_TEMP_BASED
    )
    if _calibration_type != CalibrationType.DIRECT_VALVE_BASED:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s does not support direct valve control due to calibration type %s",
            self.device_name,
            entity_id,
            _calibration_type,
        )
        return False

    trv_data = self.real_trvs.get(entity_id) or {}
    valve_entity = trv_data.get("valve_position_entity")
    writable_flag = trv_data.get("valve_position_writable")
    if valve_entity and writable_flag is True:
        return True

    quirks = trv_data.get("model_quirks")
    _override_set_valve = getattr(quirks, "override_set_valve", None)
    if callable(_override_set_valve):
        return True

    return False


def _compute_mpc_balance(self, entity_id: str):
    """Run the MPC balance algorithm for calibration purposes."""

    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return None, False

    if self.bt_target_temp is None or self.cur_temp is None:
        trv_state["calibration_balance"] = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state["calibration_balance"] = None
        return None, False

    params = MpcParams()

    # Optional: use filtered external temperature for MPC cost evaluation to reduce jitter.
    # `cur_temp_filtered` is maintained by events/temperature.py (EMA) and passed separately.
    mpc_current_temp = self.cur_temp
    mpc_filtered_temp = self.cur_temp_filtered

    _is_day = True
    if self.hass:
        _sun = self.hass.states.get("sun.sun")
        if _sun and _sun.state == "below_horizon":
            _is_day = False

    _solar_intensity = 0.0
    if _is_day:
        _solar_intensity = _get_current_solar_intensity(self)

    try:
        mpc_output = compute_mpc(
            MpcInput(
                key=build_mpc_key(self, entity_id),
                target_temp_C=self.bt_target_temp,
                current_temp_C=mpc_current_temp,
                filtered_temp_C=mpc_filtered_temp,
                trv_temp_C=trv_state.get("current_temperature"),
                tolerance_K=float(self.tolerance or 0.0),
                temp_slope_K_per_min=self.temp_slope,
                window_open=self.window_open or False,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
                outdoor_temp_C=_get_current_outdoor_temp(self),
                is_day=_is_day,
                solar_intensity=_solar_intensity,
            ),
            params,
        )
    except (ValueError, TypeError, ZeroDivisionError) as err:
        _LOGGER.debug(
            "better_thermostat %s: MPC calibration compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state["calibration_balance"] = None
        return None, False

    if mpc_output is None:
        trv_state["calibration_balance"] = None
        return None, False

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state["calibration_balance"] = {
        "valve_percent": mpc_output.valve_percent,
        "apply_valve": supports_valve,
        "debug": getattr(mpc_output, "debug", None),
    }

    _schedule_mpc = self._schedule_save_mpc_states
    if callable(_schedule_mpc):
        _schedule_mpc()

    return mpc_output, supports_valve


def _compute_tpi_balance(self, entity_id: str):
    """Run the TPI balance algorithm for calibration purposes."""

    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return None, False

    if self.bt_target_temp is None or self.cur_temp is None:
        trv_state["calibration_balance"] = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state["calibration_balance"] = None
        return None, False

    # Use default TPI params
    params = TpiParams()

    try:
        tpi_output = compute_tpi(
            TpiInput(
                key=build_tpi_key(self, entity_id),
                current_temp_C=self.cur_temp,
                target_temp_C=self.bt_target_temp,
                outdoor_temp_C=_get_current_outdoor_temp(self),
                window_open=self.window_open or False,
                heating_allowed=True,
                bt_name=self.device_name,
                entity_id=entity_id,
            ),
            params,
        )
    except (ValueError, TypeError, ZeroDivisionError) as err:
        _LOGGER.debug(
            "better_thermostat %s: TPI calibration compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state["calibration_balance"] = None
        return None, False

    if tpi_output is None:
        trv_state["calibration_balance"] = None
        return None, False

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state["calibration_balance"] = {
        "valve_percent": tpi_output.duty_cycle_pct,
        "apply_valve": supports_valve,
        "debug": getattr(tpi_output, "debug", None),
    }

    _schedule_tpi = self._schedule_save_tpi_states
    if callable(_schedule_tpi):
        _schedule_tpi()

    return tpi_output, supports_valve


def _compute_pid_balance(self, entity_id: str):
    """Run the PID balance algorithm for calibration purposes."""

    trv_state = self.real_trvs.get(entity_id)
    if trv_state is None:
        return None, False

    if self.bt_target_temp is None or self.cur_temp is None:
        trv_state["calibration_balance"] = None
        return None, False

    if self.window_open is True:
        trv_state["calibration_balance"] = None
        return None, False

    hvac_mode = self.bt_hvac_mode
    if hvac_mode == HVACMode.OFF:
        trv_state["calibration_balance"] = None
        return None, False

    # Build PID params from config and learned values
    key = build_pid_key(self, entity_id)
    pid_state = get_pid_state(key)

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

    _LOGGER.debug(
        "better_thermostat %s: Running PID calibration for %s",
        self.device_name,
        entity_id,
    )

    try:
        percent, debug = compute_pid(
            params,
            self.bt_target_temp,
            self.cur_temp,
            trv_state.get("current_temperature"),
            self.temp_slope,
            key,
        )
        # Schedule saving of updated PID states
        self.schedule_save_pid_state()
    except (ValueError, TypeError, ZeroDivisionError) as err:
        _LOGGER.debug(
            "better_thermostat %s: PID calibration compute failed for %s: %s",
            self.device_name,
            entity_id,
            err,
        )
        trv_state["calibration_balance"] = None
        return None, False

    if percent is None:
        trv_state["calibration_balance"] = None
        return None, False

    supports_valve = _supports_direct_valve_control(self, entity_id)
    trv_state["calibration_balance"] = {
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

    if self.cur_temp is None or self.bt_target_temp is None:
        return None

    # Add tolerance check
    _cur_external_temp = self.cur_temp
    _cur_target_temp = self.bt_target_temp
    _within_tolerance = _cur_external_temp >= (
        _cur_target_temp - self.tolerance
    ) and _cur_external_temp <= (_cur_target_temp + self.tolerance)

    _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
        "calibration_mode", CalibrationMode.MPC_CALIBRATION
    )

    if _within_tolerance:
        # When within tolerance, don't adjust calibration but keep MPC/TPI/PID valve data fresh
        if _calibration_mode == CalibrationMode.MPC_CALIBRATION:
            _compute_mpc_balance(self, entity_id)
        elif _calibration_mode == CalibrationMode.TPI_CALIBRATION:
            _compute_tpi_balance(self, entity_id)
        elif _calibration_mode == CalibrationMode.PID_CALIBRATION:
            _compute_pid_balance(self, entity_id)
        else:
            self.real_trvs[entity_id].pop("calibration_balance", None)
        return self.real_trvs[entity_id]["last_calibration"]

    _cur_trv_temp_s = self.real_trvs[entity_id]["current_temperature"]
    _calibration_step = self.real_trvs[entity_id]["local_calibration_step"]
    _calibration_step = _convert_to_float(_calibration_step)
    _cur_trv_temp_f = _convert_to_float(_cur_trv_temp_s)
    _current_trv_calibration = _convert_to_float(
        self.real_trvs[entity_id]["last_calibration"]
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
    _cur_target_temp = float(_cur_target_temp)
    _cur_trv_temp_f = float(_cur_trv_temp_f)
    _current_trv_calibration = float(_current_trv_calibration)
    _calibration_step = float(_calibration_step)

    _new_trv_calibration = (
        _cur_external_temp - _cur_trv_temp_f
    ) + _current_trv_calibration

    _mpc_result = None
    _mpc_use_valve = False
    if _calibration_mode == CalibrationMode.MPC_CALIBRATION:
        _mpc_result, _mpc_use_valve = _compute_mpc_balance(self, entity_id)
        if _mpc_use_valve:
            _new_trv_calibration = _current_trv_calibration
        elif _mpc_result is not None:
            _mpc_percent = getattr(_mpc_result, "valve_percent", None)
            if isinstance(_mpc_percent, (int, float)):
                _max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if _max_temp is not None:
                    _valve_fraction = max(0.0, min(1.0, float(_mpc_percent) / 100.0))
                    _desired_trv_setpoint = _cur_trv_temp_f + (
                        (float(_max_temp) - _cur_trv_temp_f) * _valve_fraction
                    )
                    _new_trv_calibration = _current_trv_calibration - (
                        _desired_trv_setpoint - _cur_target_temp
                    )
    elif _calibration_mode == CalibrationMode.TPI_CALIBRATION:
        _tpi_result, _tpi_use_valve = _compute_tpi_balance(self, entity_id)
        if _tpi_use_valve:
            _new_trv_calibration = _current_trv_calibration
        elif _tpi_result is not None:
            _tpi_percent = getattr(_tpi_result, "valve_percent", None)
            if isinstance(_tpi_percent, (int, float)):
                _max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if _max_temp is not None:
                    _valve_fraction = max(0.0, min(1.0, float(_tpi_percent) / 100.0))
                    _desired_trv_setpoint = _cur_trv_temp_f + (
                        (float(_max_temp) - _cur_trv_temp_f) * _valve_fraction
                    )
                    _new_trv_calibration = _current_trv_calibration - (
                        _desired_trv_setpoint - _cur_target_temp
                    )
    elif _calibration_mode == CalibrationMode.PID_CALIBRATION:
        _pid_result, _pid_use_valve = _compute_pid_balance(self, entity_id)
        if _pid_use_valve:
            _new_trv_calibration = _current_trv_calibration
        elif _pid_result is not None:
            _pid_percent = _pid_result
            if isinstance(_pid_percent, (int, float)):
                _max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if _max_temp is not None:
                    _valve_fraction = max(0.0, min(1.0, float(_pid_percent) / 100.0))
                    _desired_trv_setpoint = _cur_trv_temp_f + (
                        (float(_max_temp) - _cur_trv_temp_f) * _valve_fraction
                    )
                    _new_trv_calibration = _current_trv_calibration - (
                        _desired_trv_setpoint - _cur_target_temp
                    )
    else:
        self.real_trvs[entity_id].pop("calibration_balance", None)

    if _new_trv_calibration is None:
        return None

    _skip_post_adjustments = _calibration_mode in (
        CalibrationMode.MPC_CALIBRATION,
        CalibrationMode.TPI_CALIBRATION,
        CalibrationMode.PID_CALIBRATION,
    )

    _new_trv_calibration = float(_new_trv_calibration)

    if _calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
        if self.attr_hvac_action == HVACAction.HEATING:
            if _new_trv_calibration > -2.5:
                _new_trv_calibration -= 2.5

    if _calibration_mode == CalibrationMode.HEATING_POWER_CALIBRATION:
        _supports_valve = _supports_direct_valve_control(self, entity_id)
        if self.attr_hvac_action != HVACAction.HEATING:
            if _supports_valve:
                self.real_trvs[entity_id]["calibration_balance"] = {
                    "valve_percent": 0,
                    "apply_valve": True,
                    "debug": {"source": "heating_power_calibration"},
                }
                # Keep TRV calibration at BT target when we control valve directly
                _new_trv_calibration = _current_trv_calibration
                _skip_post_adjustments = True

        elif self.attr_hvac_action == HVACAction.HEATING:
            _valve_position = heating_power_valve_position(self, entity_id)

            if _supports_valve and isinstance(_valve_position, (int, float)):
                try:
                    _pct = int(max(0, min(100, round(float(_valve_position) * 100.0))))
                except (TypeError, ValueError):
                    _pct = None

                if _pct is not None:
                    # Publish valve intent so controlling layer can execute set_valve
                    self.real_trvs[entity_id]["calibration_balance"] = {
                        "valve_percent": _pct,
                        "apply_valve": True,
                        "debug": {"source": "heating_power_calibration"},
                    }
                    # Keep local calibration unchanged when we control via valve
                    _new_trv_calibration = _current_trv_calibration
                    # Skip post adjustments to avoid counteracting direct valve control
                    _skip_post_adjustments = True
                else:
                    # Fallback to legacy behavior
                    _new_trv_calibration = _current_trv_calibration - (
                        (
                            self.real_trvs[entity_id]["local_calibration_min"]
                            + _cur_trv_temp_f
                        )
                        * _valve_position
                    )
            else:
                # No direct valve support: compute calibration as before and clear any stale balance
                self.real_trvs[entity_id].pop("calibration_balance", None)
                _new_trv_calibration = _current_trv_calibration - (
                    (
                        self.real_trvs[entity_id]["local_calibration_min"]
                        + _cur_trv_temp_f
                    )
                    * _valve_position
                )
        else:
            # Not heating: ensure we don't apply stale valve instructions
            self.real_trvs[entity_id].pop("calibration_balance", None)

    # Respecting tolerance in all calibration modes, delaying heat
    if not _skip_post_adjustments:
        if self.attr_hvac_action == HVACAction.IDLE:
            if _new_trv_calibration < 0.0:
                _new_trv_calibration += self.tolerance * 2.0

    _new_trv_calibration = fix_local_calibration(self, entity_id, _new_trv_calibration)

    _overheating_protection = self.real_trvs[entity_id]["advanced"].get(
        CONF_PROTECT_OVERHEATING, False
    )

    # Additional adjustment if overheating protection is enabled
    if not _skip_post_adjustments and _overheating_protection is True:
        if self.attr_hvac_action == HVACAction.IDLE:
            _new_trv_calibration += (
                _cur_external_temp - (_cur_target_temp + self.tolerance)
            ) * 8.0  # Reduced from 10.0 since we already add 2.0

    # Adjust based on the step size allowed by the local calibration entity
    _rounded_calibration = round_by_step(_new_trv_calibration, _calibration_step)
    if _rounded_calibration is None:
        return None
    _new_trv_calibration = _rounded_calibration

    # limit new setpoint within min/max of the TRV's range
    t_min = _convert_to_float(self.real_trvs[entity_id]["local_calibration_min"])
    t_max = _convert_to_float(self.real_trvs[entity_id]["local_calibration_max"])
    if t_min is None or t_max is None:
        return _new_trv_calibration
    t_min = float(t_min)
    t_max = float(t_max)
    _new_trv_calibration = max(t_min, min(_new_trv_calibration, t_max))

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

    if self.cur_temp is None or self.bt_target_temp is None:
        return None

    # Add tolerance check
    _cur_external_temp = float(self.cur_temp)
    _cur_target_temp = float(self.bt_target_temp)

    _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
        "calibration_mode", CalibrationMode.MPC_CALIBRATION
    )

    _cur_trv_temp_s = self.real_trvs[entity_id]["current_temperature"]
    _cur_trv_temp = _convert_to_float(_cur_trv_temp_s)

    _trv_temp_step_raw = self.real_trvs[entity_id]["target_temp_step"]
    _trv_temp_step = _convert_to_float(_trv_temp_step_raw)
    if _trv_temp_step is None or _trv_temp_step <= 0:
        _trv_temp_step = 0.5

    if _cur_trv_temp is None:
        return None

    _cur_trv_temp = float(_cur_trv_temp)

    _calibrated_setpoint = (_cur_target_temp - _cur_external_temp) + _cur_trv_temp

    _mpc_result = None
    _mpc_use_valve = False
    if _calibration_mode == CalibrationMode.MPC_CALIBRATION:
        _mpc_result, _mpc_use_valve = _compute_mpc_balance(self, entity_id)
        if _mpc_use_valve:
            _calibrated_setpoint = _cur_target_temp
        elif _mpc_result is not None:
            _mpc_percent = getattr(_mpc_result, "valve_percent", None)
            if isinstance(_mpc_percent, (int, float)):
                _max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if _max_temp is not None:
                    _valve_fraction = max(0.0, min(1.0, float(_mpc_percent) / 100.0))
                    _calibrated_setpoint = _cur_trv_temp + (
                        (float(_max_temp) - _cur_trv_temp) * _valve_fraction
                    )
                    if _valve_fraction == 0.0 and _calibrated_setpoint >= _cur_trv_temp:
                        _calibrated_setpoint = _cur_trv_temp - 1.0
    elif _calibration_mode == CalibrationMode.TPI_CALIBRATION:
        _tpi_result, _tpi_use_valve = _compute_tpi_balance(self, entity_id)
        if _tpi_use_valve:
            _calibrated_setpoint = _cur_target_temp
        elif _tpi_result is not None:
            _tpi_percent = getattr(_tpi_result, "duty_cycle_pct", None)
            if isinstance(_tpi_percent, (int, float)):
                _max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if _max_temp is not None:
                    _tpi_fraction = max(0.0, min(1.0, float(_tpi_percent) / 100.0))
                    _calibrated_setpoint = _cur_trv_temp + (
                        (float(_max_temp) - _cur_trv_temp) * _tpi_fraction
                    )
                    if _tpi_fraction == 0.0 and _calibrated_setpoint >= _cur_trv_temp:
                        _calibrated_setpoint = _cur_trv_temp - 1.0
    elif _calibration_mode == CalibrationMode.PID_CALIBRATION:
        _pid_result, _pid_use_valve = _compute_pid_balance(self, entity_id)
        if _pid_use_valve:
            _calibrated_setpoint = _cur_target_temp
        elif _pid_result is not None:
            _pid_percent = _pid_result
            if isinstance(_pid_percent, (int, float)):
                _max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if _max_temp is not None:
                    _pid_fraction = max(0.0, min(1.0, float(_pid_percent) / 100.0))
                    _calibrated_setpoint = _cur_trv_temp + (
                        (float(_max_temp) - _cur_trv_temp) * _pid_fraction
                    )
                    if _pid_fraction == 0.0 and _calibrated_setpoint >= _cur_trv_temp:
                        _calibrated_setpoint = _cur_trv_temp - 1.0
    else:
        self.real_trvs[entity_id].pop("calibration_balance", None)

    _skip_post_adjustments = _calibration_mode in (
        CalibrationMode.MPC_CALIBRATION,
        CalibrationMode.TPI_CALIBRATION,
        CalibrationMode.PID_CALIBRATION,
    )

    if _calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
        if self.attr_hvac_action == HVACAction.HEATING:
            if _calibrated_setpoint - _cur_trv_temp < 2.5:
                _calibrated_setpoint += 2.5

    if _calibration_mode == CalibrationMode.HEATING_POWER_CALIBRATION:
        _supports_valve = _supports_direct_valve_control(self, entity_id)
        if self.attr_hvac_action != HVACAction.HEATING:
            if _supports_valve:
                self.real_trvs[entity_id]["calibration_balance"] = {
                    "valve_percent": 0,
                    "apply_valve": True,
                    "debug": {"source": "heating_power_calibration"},
                }
                # Keep TRV setpoint at BT target when we control valve directly
                _calibrated_setpoint = _cur_target_temp
                _skip_post_adjustments = True
            else:
                # Not heating: ensure we don't apply stale valve instructions
                self.real_trvs[entity_id].pop("calibration_balance", None)

        elif self.attr_hvac_action == HVACAction.HEATING:
            _valve_position = heating_power_valve_position(self, entity_id)
            if _supports_valve and isinstance(_valve_position, (int, float)):
                try:
                    _pct = int(max(0, min(100, round(float(_valve_position) * 100.0))))
                except (TypeError, ValueError):
                    _pct = None

                if _pct is not None:
                    # Publish valve intent so controlling layer can execute set_valve
                    self.real_trvs[entity_id]["calibration_balance"] = {
                        "valve_percent": _pct,
                        "apply_valve": True,
                        "debug": {"source": "heating_power_calibration"},
                    }
                    # Keep setpoint unchanged when we control via valve
                    _calibrated_setpoint = _cur_target_temp
                    # Skip post adjustments to avoid counteracting direct valve control
                    _skip_post_adjustments = True
                else:
                    # Fallback to legacy behavior
                    max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                    if max_temp is not None:
                        _calibrated_setpoint = _cur_trv_temp + (
                            (float(max_temp) - _cur_trv_temp) * _valve_position
                        )
            else:
                # No direct valve support: compute setpoint as before and clear any stale balance
                self.real_trvs[entity_id].pop("calibration_balance", None)
                max_temp = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
                if max_temp is not None:
                    _calibrated_setpoint = _cur_trv_temp + (
                        (float(max_temp) - _cur_trv_temp) * _valve_position
                    )
        else:
            # Not heating: ensure we don't apply stale valve instructions
            self.real_trvs[entity_id].pop("calibration_balance", None)

    if _calibrated_setpoint is None:
        return None

    _calibrated_setpoint = float(_calibrated_setpoint)

    if not _skip_post_adjustments:
        if self.attr_hvac_action == HVACAction.IDLE:
            if _calibrated_setpoint - _cur_trv_temp > 0.0:
                _calibrated_setpoint -= self.tolerance * 2.0

    _calibrated_setpoint = fix_target_temperature_calibration(
        self, entity_id, _calibrated_setpoint
    )

    _overheating_protection = self.real_trvs[entity_id]["advanced"].get(
        CONF_PROTECT_OVERHEATING, False
    )

    # Additional adjustment if overheating protection is enabled
    if not _skip_post_adjustments and _overheating_protection is True:
        if self.attr_hvac_action == HVACAction.IDLE:
            _calibrated_setpoint -= (
                _cur_external_temp - (_cur_target_temp + self.tolerance)
            ) * 8.0  # Reduced from 10.0 since we already subtract 2.0

    _rounded_setpoint = round_by_step(_calibrated_setpoint, _trv_temp_step)
    if _rounded_setpoint is None:
        return None
    _calibrated_setpoint = _rounded_setpoint

    # limit new setpoint within min/max of the TRV's range
    t_min = _convert_to_float(self.real_trvs[entity_id]["min_temp"])
    t_max = _convert_to_float(self.real_trvs[entity_id]["max_temp"])
    if t_min is not None:
        _calibrated_setpoint = max(float(t_min), _calibrated_setpoint)
    if t_max is not None:
        _calibrated_setpoint = min(_calibrated_setpoint, float(t_max))

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
