""""""
import json
import logging
import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from homeassistant.components.climate.const import SUPPORT_TARGET_TEMPERATURE
from homeassistant.const import ATTR_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


DEFAULT_NAME = "Better Thermostat"
VERSION = "master"
try:
    with open(
        "/config/custom_components/better_thermostat/manifest.json"
    ) as manifest_file:
        manifest = json.load(manifest_file)
        VERSION = manifest["version"]
except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
    _LOGGER.error("better_thermostat %s: could not read version from manifest file.", e)


CONF_HEATER = "thermostat"
CONF_SENSOR = "temperature_sensor"
CONF_SENSOR_WINDOW = "window_sensors"
CONF_TARGET_TEMP = "target_temp"
CONF_WEATHER = "weather"
CONF_OFF_TEMPERATURE = "off_temperature"
CONF_WINDOW_TIMEOUT = "window_off_delay"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_VALVE_MAINTENANCE = "valve_maintenance"
CONF_NIGHT_TEMP = "night_temp"
CONF_NIGHT_START = "night_start"
CONF_NIGHT_END = "night_end"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_PRECISION = "precision"
CONF_LOCAL_CALIBRATION = "local_calibration"
CONF_CHILD_LOCK = "child_lock"
CONF_CALIBRATIION_ROUND = "calibration_round"
CONF_HEAT_AUTO_SWAPPED = "heat_auto_swapped"
CONF_MODEL = "model"

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

ATTR_STATE_WINDOW_OPEN = "window_open"
ATTR_STATE_NIGHT_MODE = "night_mode"
ATTR_STATE_CALL_FOR_HEAT = "call_for_heat"
ATTR_STATE_LAST_CHANGE = "last_change"
ATTR_STATE_DAY_SET_TEMP = "last_day_set_temp"

ATTR_VALVE_POSITION = "valve_position"

SERVICE_SAVE_CURRENT_TARGET_TEMPERATURE = (
    "better_thermostat_save_current_target_temperature"
)
SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE = (
    "better_thermostat_restore_saved_target_temperature"
)
SERVICE_SET_TEMP_TARGET_TEMPERATURE = "better_thermostat_set_temp_target_temperature"

SERVICE_TO_METHOD = {
    SERVICE_SAVE_CURRENT_TARGET_TEMPERATURE: {
        "method": "async_save_current_target_temperature"
    },
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE: {
        "method": "async_restore_saved_target_temperature"
    },
    SERVICE_SET_TEMP_TARGET_TEMPERATURE: {
        "method": "async_set_temp_target_temperatrure"
    },
}
BETTERTHERMOSTAT_SERVICE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_ENTITY_ID): cv.entity_ids}
)
