""""""
import json
from enum import IntEnum
from homeassistant.backports.enum import StrEnum

import logging
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import (  # noqa: F401
    make_entity_service_schema,
)
from homeassistant.components.climate.const import SUPPORT_TARGET_TEMPERATURE
from homeassistant.const import ATTR_TEMPERATURE

_LOGGER = logging.getLogger(__name__)


DEFAULT_NAME = "Better Thermostat"
VERSION = "master"
try:
    with open("custom_components/better_thermostat/manifest.json") as manifest_file:
        manifest = json.load(manifest_file)
        VERSION = manifest["version"]
except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
    _LOGGER.error("better_thermostat %s: could not read version from manifest file.", e)


CONF_HEATER = "thermostat"
CONF_SENSOR = "temperature_sensor"
CONF_HUMIDITY = "humidity_sensor"
CONF_SENSOR_WINDOW = "window_sensors"
CONF_TARGET_TEMP = "target_temp"
CONF_WEATHER = "weather"
CONF_OFF_TEMPERATURE = "off_temperature"
CONF_WINDOW_TIMEOUT = "window_off_delay"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_VALVE_MAINTENANCE = "valve_maintenance"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_PRECISION = "precision"
CONF_CALIBRATION = "calibration"
CONF_CHILD_LOCK = "child_lock"
CONF_PROTECT_OVERHEATING = "protect_overheating"
CONF_CALIBRATION_MODE = "calibration_mode"
CONF_FIX_CALIBRATION = "fix_calibration"
CONF_HEATING_POWER_CALIBRATION = "heating_power_calibration"
CONF_HEAT_AUTO_SWAPPED = "heat_auto_swapped"
CONF_MODEL = "model"
CONF_HOMATICIP = "homaticip"
CONF_INTEGRATION = "integration"
CONF_NO_SYSTEM_MODE_OFF = "no_off_system_mode"
SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

ATTR_STATE_WINDOW_OPEN = "window_open"
ATTR_STATE_CALL_FOR_HEAT = "call_for_heat"
ATTR_STATE_LAST_CHANGE = "last_change"
ATTR_STATE_SAVED_TEMPERATURE = "saved_temperature"
ATTR_VALVE_POSITION = "valve_position"
ATTR_STATE_HUMIDIY = "humidity"
ATTR_STATE_MAIN_MODE = "main_mode"
ATTR_STATE_HEATING_POWER = "heating_power"
ATTR_STATE_HEATING_STATS = "heating_stats"

SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE = "restore_saved_target_temperature"
SERVICE_SET_TEMP_TARGET_TEMPERATURE = "set_temp_target_temperature"
SERVICE_RESET_HEATING_POWER = "reset_heating_power"

BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA = vol.All(
    cv.has_at_least_one_key(ATTR_TEMPERATURE),
    make_entity_service_schema(
        {vol.Exclusive(ATTR_TEMPERATURE, "temperature"): vol.Coerce(float)}
    ),
)


class BetterThermostatEntityFeature(IntEnum):
    """Supported features of the climate entity."""

    TARGET_TEMPERATURE = 1
    TARGET_TEMPERATURE_RANGE = 2


class CalibrationType(StrEnum):
    """Calibration type"""

    TARGET_TEMP_BASED = "target_temp_based"
    LOCAL_BASED = "local_calibration_based"


class CalibrationMode(StrEnum):
    """Calibration mode."""

    DEFAULT = "default"
    FIX_CALIBRATION = "fix_calibration"
    HEATING_POWER_CALIBRATION = "heating_power_calibration"
