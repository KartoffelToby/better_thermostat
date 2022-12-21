"""Helper functions for the Better Thermostat component."""
import re
import logging
from datetime import datetime
from typing import Union
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import async_entries_for_config_entry

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.utils.model_quirks import (
    fix_local_calibration,
    fix_target_temperature_calibration,
)


from ..const import (
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HEATING_POWER_CALIBRATION,
    CONF_FIX_CALIBRATION,
    CONF_PROTECT_OVERHEATING,
)


_LOGGER = logging.getLogger(__name__)


def mode_remap(self, entity_id, hvac_mode: str, inbound: bool = False) -> str:
    """Remap HVAC mode to correct mode if nessesary.

    Parameters
    ----------
    self :
            FIXME
    hvac_mode : str
            HVAC mode to be remapped

    inbound : bool
            True if the mode is coming from the device, False if it is coming from the HA.

    Returns
    -------
    str
            remapped mode according to device's quirks
    """
    _heat_auto_swapped = self.real_trvs[entity_id]["advanced"].get(
        CONF_HEAT_AUTO_SWAPPED, False
    )

    if _heat_auto_swapped:
        if hvac_mode == HVACMode.HEAT and inbound is False:
            return HVACMode.AUTO
        elif hvac_mode == HVACMode.AUTO and inbound is True:
            return HVACMode.HEAT
        else:
            return hvac_mode
    else:
        if hvac_mode != HVACMode.AUTO:
            return hvac_mode
        else:
            _LOGGER.error(
                f"better_thermostat {self.name}: {entity_id} HVAC mode {hvac_mode} is not supported by this device, is it possible that you forgot to set the heat auto swapped option?"
            )
            return HVACMode.OFF


def calculate_local_setpoint_delta(self, entity_id) -> Union[float, None]:
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
    _context = "calculate_local_setpoint_delta()"

    if None in (self.cur_temp, self.bt_target_temp, self.old_internal_temp):
        return None

    # check if we need to calculate
    if (
        self.real_trvs[entity_id]["current_temperature"] == self.old_internal_temp
        and self.cur_temp == self.old_external_temp
    ):
        return None

    _cur_trv_temp = convert_to_float(
        str(self.real_trvs[entity_id]["current_temperature"]), self.name, _context
    )

    _calibration_delta = float(
        str(format(float(abs(_cur_trv_temp - self.cur_temp)), ".1f"))
    )

    if _calibration_delta <= 0.5:
        return None

    self.old_internal_temp = self.real_trvs[entity_id]["current_temperature"]
    self.old_external_temp = self.cur_temp

    _current_trv_calibration = round_to_half_degree(
        convert_to_float(
            str(self.real_trvs[entity_id]["last_calibration"]), self.name, _context
        )
    )

    if None in (_current_trv_calibration, self.cur_temp, _cur_trv_temp):
        _LOGGER.warning(
            f"better thermostat {self.name}: {entity_id} Could not calculate local setpoint delta in {_context}:"
            f" current_trv_calibration: {_current_trv_calibration}, current_trv_temp: {_cur_trv_temp}, cur_temp: {self.cur_temp}"
        )
        return None

    _new_local_calibration = (self.cur_temp - _cur_trv_temp) + _current_trv_calibration

    _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
        "calibration_mode", "default"
    )
    if _calibration_mode == CONF_FIX_CALIBRATION:
        _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))
        if _temp_diff > 0.30 and _new_local_calibration > -2.5:
            _new_local_calibration -= 2.5

    elif _calibration_mode == CONF_HEATING_POWER_CALIBRATION:
        _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))
        if _temp_diff > 0.0:
            valve_position = heating_power_valve_position(self, entity_id)
            _new_local_calibration = _current_trv_calibration - (
                (self.real_trvs[entity_id]["local_calibration_min"] + _cur_trv_temp)
                * valve_position
            )

    _new_local_calibration = fix_local_calibration(
        self, entity_id, _new_local_calibration
    )

    _overheating_protection = self.real_trvs[entity_id]["advanced"].get(
        CONF_PROTECT_OVERHEATING, False
    )

    if _overheating_protection is True:
        if (self.cur_temp + 0.10) >= self.bt_target_temp:
            _new_local_calibration += 1.0

    _new_local_calibration = round_down_to_half_degree(_new_local_calibration)

    if _new_local_calibration > float(
        self.real_trvs[entity_id]["local_calibration_max"]
    ):
        _new_local_calibration = float(
            self.real_trvs[entity_id]["local_calibration_max"]
        )
    elif _new_local_calibration < float(
        self.real_trvs[entity_id]["local_calibration_min"]
    ):
        _new_local_calibration = float(
            self.real_trvs[entity_id]["local_calibration_min"]
        )

    _new_local_calibration = convert_to_float(
        str(_new_local_calibration), self.name, _context
    )

    _LOGGER.debug(
        "better_thermostat %s: %s - output calib: %s",
        self.name,
        entity_id,
        _new_local_calibration,
    )

    return convert_to_float(str(_new_local_calibration), self.name, _context)


def calculate_setpoint_override(self, entity_id) -> Union[float, None]:
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

    _cur_trv_temp = self.hass.states.get(entity_id).attributes["current_temperature"]
    if None in (self.bt_target_temp, self.cur_temp, _cur_trv_temp):
        return None

    _calibrated_setpoint = (self.bt_target_temp - self.cur_temp) + _cur_trv_temp

    _calibration_mode = self.real_trvs[entity_id]["advanced"].get(
        "calibration_mode", "default"
    )
    if _calibration_mode == CONF_FIX_CALIBRATION:
        _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))
        if _temp_diff > 0.0 and _calibrated_setpoint - _cur_trv_temp < 2.5:
            _calibrated_setpoint += 2.5

    elif _calibration_mode == CONF_HEATING_POWER_CALIBRATION:
        _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))
        if _temp_diff > 0.0:
            valve_position = heating_power_valve_position(self, entity_id)
            _calibrated_setpoint = _cur_trv_temp + (
                (self.real_trvs[entity_id]["max_temp"] - _cur_trv_temp) * valve_position
            )

    _calibrated_setpoint = fix_target_temperature_calibration(
        self, entity_id, _calibrated_setpoint
    )

    _overheating_protection = self.real_trvs[entity_id]["advanced"].get(
        CONF_PROTECT_OVERHEATING, False
    )

    if _overheating_protection is True:
        if (self.cur_temp + 0.10) >= self.bt_target_temp:
            _calibrated_setpoint -= 1.0

    _calibrated_setpoint = round_down_to_half_degree(_calibrated_setpoint)

    # check if new setpoint is inside the TRV's range, else set to min or max
    if _calibrated_setpoint < self.real_trvs[entity_id]["min_temp"]:
        _calibrated_setpoint = self.real_trvs[entity_id]["min_temp"]
    if _calibrated_setpoint > self.real_trvs[entity_id]["max_temp"]:
        _calibrated_setpoint = self.real_trvs[entity_id]["max_temp"]

    return _calibrated_setpoint


def heating_power_valve_position(self, entity_id):
    _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))
    valve_pos = (_temp_diff / self.heating_power) / 100
    if valve_pos < 0.0:
        valve_pos = 0.0
    if valve_pos > 1.0:
        valve_pos = 1.0

    _LOGGER.debug(
        f"better_thermostat {self.name}: {entity_id} / heating_power_valve_position - temp diff: {round(_temp_diff, 1)} - heating power: {round(self.heating_power, 4)} - expected valve position: {round(valve_pos * 100)}%"
    )
    return valve_pos


def convert_to_float(
    value: Union[str, int, float], instance_name: str, context: str
) -> Union[float, None]:
    """Convert value to float or print error message.

    Parameters
    ----------
    value : str, int, float
            the value to convert to float
    instance_name : str
            the name of the instance thermostat
    context : str
            the name of the function which is using this, for printing an error message

    Returns
    -------
    float
            the converted value
    None
            If error occurred and cannot convert the value.
    """
    if isinstance(value, float):
        return round(value, 1)
    elif value is None or value == "None":
        return None
    else:
        try:
            return round(float(str(format(float(value), ".1f"))), 1)
        except (ValueError, TypeError, AttributeError, KeyError):
            _LOGGER.debug(
                f"better thermostat {instance_name}: Could not convert '{value}' to float in {context}"
            )
            return None


def calibration_round(value: Union[int, float, None]) -> Union[float, int, None]:
    """Round the calibration value to the nearest 0.5.

    Parameters
    ----------
    value : float
            the value to round

    Returns
    -------
    float
            the rounded value
    """
    if value is None:
        return None
    split = str(float(str(value))).split(".", 1)
    decimale = int(split[1])
    if decimale > 8:
        return float(str(split[0])) + 1.0
    else:
        return float(str(split[0]))


def round_down_to_half_degree(
    value: Union[int, float, None]
) -> Union[float, int, None]:
    """Round the value down to the nearest 0.5.

    Parameters
    ----------
    value : float
            the value to round

    Returns
    -------
    float
            the rounded value
    """
    if value is None:
        return None
    split = str(float(str(value))).split(".", 1)
    decimale = int(split[1])
    if decimale >= 5:
        if float(split[0]) > 0:
            return float(str(split[0])) + 0.5
        else:
            return float(str(split[0])) - 0.5
    else:
        return float(str(split[0]))


def round_to_half_degree(value: Union[int, float, None]) -> Union[float, int, None]:
    """Rounds numbers to the nearest n.5/n.0

    Parameters
    ----------
    value : int, float
            input value

    Returns
    -------
    float, int
            either an int, if input was an int, or a float rounded to n.5/n.0

    """
    if value is None:
        return None
    elif isinstance(value, float):
        return round(value * 2) / 2
    elif isinstance(value, int):
        return value


def round_to_hundredth_degree(
    value: Union[int, float, None]
) -> Union[float, int, None]:
    """Rounds numbers to the nearest n.nn0

    Parameters
    ----------
    value : int, float
            input value

    Returns
    -------
    float, int
            either an int, if input was an int, or a float rounded to n.nn0

    """
    if value is None:
        return None
    elif isinstance(value, float):
        return round(value * 100) / 100
    elif isinstance(value, int):
        return value


def check_float(potential_float):
    """Check if a string is a float.

    Parameters
    ----------
    potential_float :
            the value to check

    Returns
    -------
    bool
            True if the value is a float, False otherwise.

    """
    try:
        float(potential_float)
        return True
    except ValueError:
        return False


def convert_time(time_string):
    """Convert a time string to a datetime object.

    Parameters
    ----------
    time_string :
            a string representing a time

    Returns
    -------
    datetime
            the converted time as a datetime object.
    None
            If the time string is not a valid time.
    """
    try:
        _current_time = datetime.now()
        _get_hours_minutes = datetime.strptime(time_string, "%H:%M")
        return _current_time.replace(
            hour=_get_hours_minutes.hour,
            minute=_get_hours_minutes.minute,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None


async def find_valve_entity(self, entity_id):
    """Find the local calibration entity for the TRV.

    This is a hacky way to find the local calibration entity for the TRV. It is not possible to find the entity
    automatically, because the entity_id is not the same as the friendly_name. The friendly_name is the same for all
    thermostats of the same brand, but the entity_id is different.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    str
            the entity_id of the local calibration entity
    None
            if no local calibration entity was found
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
    for entity in entity_entries:
        uid = entity.unique_id
        # Make sure we use the correct device entities
        if entity.device_id == reg_entity.device_id:
            if "_valve_position" in uid or "_position" in uid:
                _LOGGER.debug(
                    f"better thermostat: Found valve position entity {entity.entity_id} for {entity_id}"
                )
                return entity.entity_id

    _LOGGER.debug(
        f"better thermostat: Could not find valve position entity for {entity_id}"
    )
    return None


async def find_local_calibration_entity(self, entity_id):
    """Find the local calibration entity for the TRV.

    This is a hacky way to find the local calibration entity for the TRV. It is not possible to find the entity
    automatically, because the entity_id is not the same as the friendly_name. The friendly_name is the same for all
    thermostats of the same brand, but the entity_id is different.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    str
            the entity_id of the local calibration entity
    None
            if no local calibration entity was found
    """
    entity_registry = er.async_get(self.hass)
    reg_entity = entity_registry.async_get(entity_id)
    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
    for entity in entity_entries:
        uid = entity.unique_id
        # Make sure we use the correct device entities
        if entity.device_id == reg_entity.device_id:
            if "local_temperature_calibration" in uid:
                _LOGGER.debug(
                    f"better thermostat: Found local calibration entity {entity.entity_id} for {entity_id}"
                )
                return entity.entity_id

    _LOGGER.debug(
        f"better thermostat: Could not find local calibration entity for {entity_id}"
    )
    return None


async def get_trv_intigration(self, entity_id):
    """Get the integration of the TRV.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    str
            the integration of the TRV
    """
    entity_reg = er.async_get(self.hass)
    entry = entity_reg.async_get(entity_id)
    try:
        return entry.platform
    except AttributeError:
        return "generic_thermostat"


def get_max_value(obj, value, default):
    """Get the max value of an dict object."""
    try:
        _raw = []
        for key in obj.keys():
            _temp = obj[key].get(value, 0)
            if _temp is not None:
                _raw.append(_temp)
        return max(_raw, key=lambda x: float(x))
    except (KeyError, ValueError):
        return default


def get_min_value(obj, value, default):
    """Get the min value of an dict object."""
    try:
        _raw = []
        for key in obj.keys():
            _temp = obj[key].get(value, 999)
            if _temp is not None:
                _raw.append(_temp)
        return min(_raw, key=lambda x: float(x))
    except (KeyError, ValueError):
        return default


async def get_device_model(self, entity_id):
    """Fetches the device model from HA.
    Parameters
    ----------
    self :
            self instance of better_thermostat
    Returns
    -------
    string
            the name of the thermostat model
    """
    if self.model is None:
        try:
            entity_reg = er.async_get(self.hass)
            entry = entity_reg.async_get(entity_id)
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get(entry.device_id)
            _LOGGER.debug(f"better_thermostat {self.name}: found device:")
            _LOGGER.debug(device)
            try:
                # Z2M reports the device name as a long string with the actual model name in braces, we need to extract it
                return re.search("\\((.+?)\\)", device.model).group(1)
            except AttributeError:
                # Other climate integrations might report the model name plainly, need more infos on this
                return device.model
        except (
            RuntimeError,
            ValueError,
            AttributeError,
            KeyError,
            TypeError,
            NameError,
            IndexError,
        ):
            try:
                return (
                    self.hass.states.get(entity_id)
                    .attributes.get("device")
                    .get("model", "generic")
                )
            except (
                RuntimeError,
                ValueError,
                AttributeError,
                KeyError,
                TypeError,
                NameError,
                IndexError,
            ):
                return "generic"
    else:
        return self.model
