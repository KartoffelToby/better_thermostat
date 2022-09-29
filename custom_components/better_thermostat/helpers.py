"""Helper functions for the Better Thermostat component."""
import re
import logging
from datetime import datetime
from typing import Union
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import async_entries_for_config_entry

from homeassistant.components.climate.const import HVAC_MODE_AUTO, HVAC_MODE_HEAT


_LOGGER = logging.getLogger(__name__)


def log_info(self, message):
    """Log a message to the info log.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    message :
            the message to log

    Returns
    -------
    None
    """
    _LOGGER.debug(
        "better_thermostat with config name: %s, %s TRV: %s",
        self.name,
        message,
        self.hass.states.get(self.heater_entity_id)
        .attributes.get("device")
        .get("friendlyName"),
    )


def mode_remap(self, hvac_mode: str, inbound: bool = False) -> str:
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
    _heat_auto_swapped = self._config.get("heat_auto_swapped")
    if _heat_auto_swapped:
        if hvac_mode == HVAC_MODE_HEAT and not inbound:
            return HVAC_MODE_AUTO
        elif hvac_mode == HVAC_MODE_AUTO and inbound:
            return HVAC_MODE_HEAT
    else:
        if hvac_mode == HVAC_MODE_AUTO and inbound:
            return HVAC_MODE_HEAT
        return hvac_mode


def calculate_local_setpoint_delta(self) -> Union[float, None]:
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

    _trv_state_attributes = self.hass.states.get(self.heater_entity_id).attributes
    _calibration_state = self.hass.states.get(
        self.local_temperature_calibration_entity
    ).state
    _context = "calculate_local_setpoint_delta()"

    _current_trv_temp = convert_to_float(
        _trv_state_attributes.get("current_temperature"), self.name, _context
    )
    _current_trv_calibration = convert_to_float(_calibration_state, self.name, _context)

    if None in (_current_trv_calibration, self._cur_temp, _current_trv_temp):
        _LOGGER.warning(
            f"better thermostat {self.name}: Could not calculate local setpoint delta in {_context}:"
            f" current_trv_calibration: {_current_trv_calibration}, current_trv_temp: {_current_trv_temp}, cur_temp: {self._cur_temp}"
        )
        return None

    _new_local_calibration = (
        self._cur_temp - _current_trv_temp + _current_trv_calibration
    )
    return _new_local_calibration


def calculate_setpoint_override(self) -> Union[float, None]:
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
    state = self.hass.states.get(self.heater_entity_id).attributes

    _context = "calculate_setpoint_override()"

    _current_trv_temp = convert_to_float(
        state.get("current_temperature"), self.name, _context
    )

    if None in (self._target_temp, self._cur_temp, _current_trv_temp):
        return None

    _calibrated_setpoint = round_to_half_degree(
        self._target_temp - self._cur_temp + _current_trv_temp
    )

    # check if new setpoint is inside the TRV's range, else set to min or max
    if _calibrated_setpoint < self._TRV_min_temp:
        _calibrated_setpoint = self._TRV_min_temp
    if _calibrated_setpoint > self._TRV_max_temp:
        _calibrated_setpoint = self._TRV_max_temp
    self._last_send_target_temp = _calibrated_setpoint
    return _calibrated_setpoint


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
        return value
    else:
        try:
            return float(value)
        except (ValueError, TypeError, AttributeError, KeyError):
            _LOGGER.debug(
                f"better thermostat {instance_name}: Could not convert '{value}' to float in {context}"
            )
            return None


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


async def find_local_calibration_entity(self):
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
    reg_entity = entity_registry.async_get(self.heater_entity_id)
    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
    for entity in entity_entries:
        uid = entity.unique_id
        # Make sure we use the correct device entities
        if entity.device_id == reg_entity.device_id:
            if "local_temperature_calibration" in uid:
                _LOGGER.debug(
                    f"better thermostat: Found local calibration entity {entity.entity_id} for {self.heater_entity_id}"
                )
                return entity.entity_id

    _LOGGER.debug(
        f"better thermostat: Could not find local calibration entity for {self.heater_entity_id}"
    )
    return None


async def get_trv_intigration(self):
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
    entry = entity_reg.async_get(self.heater_entity_id)
    try:
        return entry.platform
    except AttributeError:
        return "generic_thermostat"


async def get_device_model(self):
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
            entry = entity_reg.async_get(self.heater_entity_id)
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
                    self.hass.states.get(self.heater_entity_id)
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
