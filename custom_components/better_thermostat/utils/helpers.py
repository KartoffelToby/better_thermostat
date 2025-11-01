"""Helper functions for the Better Thermostat component."""

import re
import logging
import math
from datetime import datetime
from enum import Enum
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_registry import async_entries_for_config_entry

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.utils.const import CONF_HEAT_AUTO_SWAPPED


_LOGGER = logging.getLogger(__name__)


def get_hvac_bt_mode(self, mode: str) -> str:
    if mode == HVACMode.HEAT:
        mode = self.map_on_hvac_mode
    elif mode == HVACMode.HEAT_COOL:
        mode = HVACMode.HEAT
    return mode


def normalize_hvac_mode(value: HVACMode | str) -> HVACMode | str:
    """Normalize a hvac_mode value to a proper HVACMode enum when possible.

    Accepts
    -------
    value : HVACMode | str
        - HVACMode enum: returned as-is
        - Strings like 'heat', 'off', 'heat_cool', 'auto', 'dry', 'fan_only'
        - Strings like 'HVACMode.HEAT' (will be converted to HVACMode.HEAT)

    Returns
    -------
    HVACMode | str
        HVACMode if recognized, otherwise the lowercased string without prefix.
    """
    if isinstance(value, HVACMode):
        return value
    if isinstance(value, str):
        raw = value.strip()
        # Strip enum-like prefix if present
        if raw.lower().startswith("hvacmode."):
            raw = raw.split(".", 1)[1]
        key = raw.lower()
        mapping = {
            "off": HVACMode.OFF,
            "heat": HVACMode.HEAT,
            "cool": HVACMode.COOL,
            "heat_cool": HVACMode.HEAT_COOL,
            "auto": HVACMode.AUTO,
            "dry": HVACMode.DRY,
            "fan_only": HVACMode.FAN_ONLY,
        }
        return mapping.get(key, key)
    return value


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
        if hvac_mode == HVACMode.HEAT and not inbound:
            return HVACMode.AUTO
        if hvac_mode == HVACMode.AUTO and inbound:
            return HVACMode.HEAT
        return hvac_mode

    trv_modes = self.real_trvs[entity_id]["hvac_modes"]
    if HVACMode.HEAT not in trv_modes and HVACMode.HEAT_COOL in trv_modes:
        # entity only supports HEAT_COOL, but not HEAT - need to translate
        if not inbound and hvac_mode == HVACMode.HEAT:
            return HVACMode.HEAT_COOL
        if inbound and hvac_mode == HVACMode.HEAT_COOL:
            return HVACMode.HEAT

    if hvac_mode != HVACMode.AUTO:
        return hvac_mode

    _LOGGER.error(
        f"better_thermostat {self.device_name}: {entity_id} HVAC mode {
            hvac_mode} is not supported by this device, is it possible that you forgot to set the heat auto swapped option?"
    )
    return HVACMode.OFF


def heating_power_valve_position(self, entity_id):
    _temp_diff = float(float(self.bt_target_temp) - float(self.cur_temp))

    a = 0.019
    b = 0.946
    valve_pos = a * (_temp_diff / self.heating_power) ** b

    if valve_pos < 0.0:
        valve_pos = 0.0
    if valve_pos > 1.0:
        valve_pos = 1.0

    _LOGGER.debug(
        f"better_thermostat {self.device_name}: {entity_id} / heating_power_valve_position - temp diff: {round(
            _temp_diff, 1)} - heating power: {round(self.heating_power, 4)} - expected valve position: {round(valve_pos * 100)}%"
    )
    return valve_pos

    # Example values for different heating_power and temp_diff:
    # With heating_power of 0.02:
    # | temp_diff | valve_pos  |
    # |-----------|------------|
    # | 0.1       | 0.0871     |
    # | 0.2       | 0.1678     |
    # | 0.3       | 0.2462     |
    # | 0.4       | 0.3232     |
    # | 0.5       | 0.3992     |

    # With heating_power of 0.01:
    # | temp_diff | valve_pos  |
    # |-----------|------------|
    # | 0.1       | 0.1678     |
    # | 0.2       | 0.3232     |
    # | 0.3       | 0.4744     |
    # | 0.4       | 0.6227     |
    # | 0.5       | 0.7691     |

    # With heating_power of 0.005:
    # | temp_diff | valve_pos  |
    # |-----------|------------|
    # | 0.1       | 0.3232     |
    # | 0.2       | 0.6227     |
    # | 0.3       | 0.9139     |
    # | 0.4       | 1.0000     |
    # | 0.5       | 1.0000     |


def convert_to_float(
    value: str | float, instance_name: str, context: str
) -> float | None:
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
    if value is None or value == "None":
        return None
    try:
        return round_by_step(float(value), 0.1)
    except (ValueError, TypeError, AttributeError, KeyError):
        _LOGGER.debug(
            f"better thermostat {instance_name}: Could not convert '{
                value}' to float in {context}"
        )
        return None


class rounding(Enum):
    # rounding functions that avoid errors due to using floats

    def up(x: float) -> float:
        return math.ceil(x - 0.0001)

    def down(x: float) -> float:
        return math.floor(x + 0.0001)

    def nearest(x: float) -> float:
        return round(x - 0.0001)


def round_by_step(
    value: float | None, step: float | None, f_rounding: rounding = rounding.nearest
) -> float | None:
    """Round the value based on the allowed decimal 'step' size.

    Parameters
    ----------
    value : float
            the value to round
    step : float
            size of one step

    Returns
    -------
    float
            the rounded value
    """

    if value is None or step is None:
        return None
    # convert to integer number of steps for rounding, then convert back to decimal
    return f_rounding(value / step) * step


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
    if reg_entity is None:
        return None
    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
    for entity in entity_entries:
        uid = entity.unique_id
        # Make sure we use the correct device entities
        if entity.device_id == reg_entity.device_id:
            if "_valve_position" in uid or "_position" in uid:
                _LOGGER.debug(
                    f"better thermostat: Found valve position entity {
                        entity.entity_id} for {entity_id}"
                )
                return entity.entity_id

    _LOGGER.debug(
        f"better thermostat: Could not find valve position entity for {entity_id}"
    )
    return None


async def find_battery_entity(self, entity_id):
    entity_registry = er.async_get(self.hass)

    entity_info = entity_registry.entities.get(entity_id)

    if entity_info is None:
        return None

    device_id = entity_info.device_id

    for entity in entity_registry.entities.values():
        if entity.device_id == device_id and (
            entity.device_class == "battery"
            or entity.original_device_class == "battery"
        ):
            return entity.entity_id

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
    if reg_entity is None:
        return None
    entity_entries = async_entries_for_config_entry(
        entity_registry, reg_entity.config_entry_id
    )
    for entity in entity_entries:
        uid = entity.unique_id + " " + entity.entity_id
        # Make sure we use the correct device entities
        if entity.device_id == reg_entity.device_id:
            if (
                "temperature_calibration" in uid
                or "temperature_offset" in uid
                or "temperatur_offset" in uid
            ):
                _LOGGER.debug(
                    f"better thermostat: Found local calibration entity {
                        entity.entity_id} for {entity_id}"
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
    """Determine the device model from the Device Registry entry.

    Priority:
    1) device.model_id
    2) Model from parentheses in device.model (e.g., "Foo (TRVZB)")
    3) device.model (plain string)
    4) Fallback: self.model (Config)
    5) Fallback: "generic"
    """
    selected: str | None = None
    source: str = "none"

    try:
        entity_reg = er.async_get(self.hass)
        entry = entity_reg.async_get(entity_id)
        dev_reg = dr.async_get(self.hass)
        device = None
        try:
            dev_id = getattr(entry, "device_id", None)
            if isinstance(dev_id, str) and dev_id:
                device = dev_reg.async_get(dev_id)
        except Exception:
            device = None
        # Selection exclusively via Device-Registry
        try:
            _LOGGER.debug(
                "better_thermostat %s: device registry -> manufacturer=%s model=%s model_id=%s name=%s identifiers=%s",
                self.device_name,
                getattr(device, "manufacturer", None),
                getattr(device, "model", None),
                getattr(device, "model_id", None),
                getattr(device, "name", None),
                list(getattr(device, "identifiers", []) or []),
            )
        except Exception:
            pass

        dev_model_id = getattr(device, "model_id", None)
        if isinstance(dev_model_id, str) and len(dev_model_id.strip()) >= 2:
            selected = dev_model_id.strip()
            source = "devreg.model_id"
        else:
            model_str = getattr(device, "model", None)
            _LOGGER.debug(
                "better_thermostat %s: device.model raw='%s'",
                self.device_name,
                model_str,
            )
            matches = re.findall(r"\((.+?)\)", model_str or "")
            if matches:
                selected = matches[-1].strip()
                source = "devreg.model(parens)"
            elif isinstance(model_str, str) and len(model_str.strip()) >= 2:
                selected = model_str.strip()
                source = "devreg.model"
    except Exception:
        # swallow registry access issues and continue to fallback
        pass

    # Final fallback: configured model, then generic
    if not selected and isinstance(self.model, str) and len(self.model.strip()) >= 2:
        selected = self.model.strip()
        source = "config.model"
    if not selected:
        selected = "generic"
        source = "default"

    _LOGGER.debug(
        "better_thermostat %s: get_device_model(%s) selected='%s' via %s",
        self.device_name,
        entity_id,
        selected,
        source,
    )
    return selected
