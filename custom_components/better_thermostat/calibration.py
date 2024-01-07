"""Helper functions for the Better Thermostat component."""
import logging
from typing import Union

from homeassistant.components.climate.const import HVACAction

from custom_components.better_thermostat.utils.const import (
    CalibrationMode,
    CONF_PROTECT_OVERHEATING,
)

from custom_components.better_thermostat.utils.helpers import (
    convert_to_float,
    round_down_to_half_degree,
    round_by_steps,
    heating_power_valve_position,
)

from custom_components.better_thermostat.model_fixes.model_quirks import (
    fix_local_calibration,
    fix_target_temperature_calibration,
)

_LOGGER = logging.getLogger(__name__)


def calculate_calibration_local(self, entity_id) -> Union[float, None]:
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

    if None in (self.cur_temp, self.bt_target_temp):
        return None

    _cur_trv_temp_s = self.real_trvs[entity_id]["current_temperature"]
    _calibration_steps = self.real_trvs[entity_id]["local_calibration_steps"]
    _cur_external_temp = self.cur_temp
    _cur_target_temp = self.bt_target_temp

    _cur_trv_temp_f = convert_to_float(str(_cur_trv_temp_s), self.name, _context)

    _current_trv_calibration = convert_to_float(
        str(self.real_trvs[entity_id]["last_calibration"]), self.name, _context
    )

    if None in (
        _current_trv_calibration,
        _cur_external_temp,
        _cur_trv_temp_f,
        _calibration_steps,
    ):
        _LOGGER.warning(
            f"better thermostat {self.name}: {entity_id} Could not calculate local calibration in {_context}:"
            f" trv_calibration: {_current_trv_calibration}, trv_temp: {_cur_trv_temp_f}, external_temp: {_cur_external_temp}"
            f" calibration_steps: {_calibration_steps}"
        )
        return None

    _new_trv_calibration = (
        _cur_external_temp - _cur_trv_temp_f
    ) + _current_trv_calibration

    _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
        "calibration_mode", CalibrationMode.DEFAULT
    )

    if _calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
        if self.attr_hvac_action == HVACAction.HEATING:
            if _new_trv_calibration > -2.5:
                _new_trv_calibration -= 2.5

    if _calibration_mode == CalibrationMode.HEATING_POWER_CALIBRATION:
        if self.attr_hvac_action == HVACAction.HEATING:
            _valve_position = heating_power_valve_position(self, entity_id)
            _new_trv_calibration = _current_trv_calibration - (
                (self.real_trvs[entity_id]["local_calibration_min"] + _cur_trv_temp_f)
                * _valve_position
            )

    # Respecting tolerance in all calibration modes, delaying heat
    if self.attr_hvac_action == HVACAction.IDLE:
        if _new_trv_calibration < 0.0:
            _new_trv_calibration += self.tolerance

    _new_trv_calibration = fix_local_calibration(self, entity_id, _new_trv_calibration)

    _overheating_protection = self.real_trvs[entity_id]["advanced"].get(
        CONF_PROTECT_OVERHEATING, False
    )

    if _overheating_protection is True:
        if _cur_external_temp >= _cur_target_temp:
            _new_trv_calibration += (_cur_external_temp - _cur_target_temp) * 10.0

    # Adjust based on the steps allowed by the local calibration entity
    _new_trv_calibration = round_by_steps(_new_trv_calibration, _calibration_steps)

    # Compare against min/max
    if _new_trv_calibration > float(self.real_trvs[entity_id]["local_calibration_max"]):
        _new_trv_calibration = float(self.real_trvs[entity_id]["local_calibration_max"])
    elif _new_trv_calibration < float(
        self.real_trvs[entity_id]["local_calibration_min"]
    ):
        _new_trv_calibration = float(self.real_trvs[entity_id]["local_calibration_min"])

    _new_trv_calibration = convert_to_float(
        str(_new_trv_calibration), self.name, _context
    )

    _logmsg = (
        "better_thermostat %s: %s - new local calibration: %s | external_temp: %s, "
        "trv_temp: %s, calibration: %s"
    )

    _LOGGER.debug(
        _logmsg,
        self.name,
        entity_id,
        _new_trv_calibration,
        _cur_external_temp,
        _cur_trv_temp_f,
        _current_trv_calibration,
    )

    return _new_trv_calibration


def calculate_calibration_setpoint(self, entity_id) -> Union[float, None]:
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
    if None in (self.cur_temp, self.bt_target_temp):
        return None

    _cur_trv_temp_s = self.real_trvs[entity_id]["current_temperature"]

    _cur_external_temp = self.cur_temp
    _cur_target_temp = self.bt_target_temp

    if None in (_cur_target_temp, _cur_external_temp, _cur_trv_temp_s):
        return None

    _calibrated_setpoint = (_cur_target_temp - _cur_external_temp) + _cur_trv_temp_s

    _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
        "calibration_mode", CalibrationMode.DEFAULT
    )

    if _calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
        if self.attr_hvac_action == HVACAction.HEATING:
            if _calibrated_setpoint - _cur_trv_temp_s < 2.5:
                _calibrated_setpoint += 2.5

    if _calibration_mode == CalibrationMode.HEATING_POWER_CALIBRATION:
        if self.attr_hvac_action == HVACAction.HEATING:
            valve_position = heating_power_valve_position(self, entity_id)
            _calibrated_setpoint = _cur_trv_temp_s + (
                (self.real_trvs[entity_id]["max_temp"] - _cur_trv_temp_s)
                * valve_position
            )

    if self.attr_hvac_action == HVACAction.IDLE:
        if _calibrated_setpoint - _cur_trv_temp_s > 0.0:
            _calibrated_setpoint -= self.tolerance

    _calibrated_setpoint = fix_target_temperature_calibration(
        self, entity_id, _calibrated_setpoint
    )

    _overheating_protection = self.real_trvs[entity_id]["advanced"].get(
        CONF_PROTECT_OVERHEATING, False
    )

    if _overheating_protection is True:
        if _cur_external_temp >= _cur_target_temp:
            _calibrated_setpoint -= (_cur_external_temp - _cur_target_temp) * 10.0

    _calibrated_setpoint = round_down_to_half_degree(_calibrated_setpoint)

    # check if new setpoint is inside the TRV's range, else set to min or max
    if _calibrated_setpoint < self.real_trvs[entity_id]["min_temp"]:
        _calibrated_setpoint = self.real_trvs[entity_id]["min_temp"]
    if _calibrated_setpoint > self.real_trvs[entity_id]["max_temp"]:
        _calibrated_setpoint = self.real_trvs[entity_id]["max_temp"]

    _logmsg = (
        "better_thermostat %s: %s - new setpoint calibration: %s | external_temp: %s, "
        "target_temp: %s, trv_temp: %s"
    )

    _LOGGER.debug(
        _logmsg,
        self.name,
        entity_id,
        _calibrated_setpoint,
        _cur_external_temp,
        _cur_target_temp,
        _cur_trv_temp_s,
    )

    return _calibrated_setpoint
