"""Constants for Better Thermostat."""

from enum import IntEnum, StrEnum
import json
import logging
import os

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import ATTR_TEMPERATURE
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import make_entity_service_schema
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


DEFAULT_NAME = "Better Thermostat"
VERSION = "master"
try:
    with open(
        f"{os.path.dirname(os.path.realpath(__file__))}/../manifest.json"
    ) as manifest_file:
        manifest = json.load(manifest_file)
        VERSION = manifest["version"]
except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
    _LOGGER.error("better_thermostat %s: could not read version from manifest file.", e)


CONF_HEATER = "thermostat"
CONF_COOLER = "cooler"
CONF_SENSOR = "temperature_sensor"
CONF_HUMIDITY = "humidity_sensor"
CONF_SENSOR_WINDOW = "window_sensors"
CONF_TARGET_TEMP = "target_temp"
CONF_WEATHER = "weather"
CONF_OFF_TEMPERATURE = "off_temperature"
CONF_WINDOW_TIMEOUT = "window_off_delay"
CONF_WINDOW_TIMEOUT_AFTER = "window_off_delay_after"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_VALVE_MAINTENANCE = "valve_maintenance"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_PRECISION = "precision"
CONF_CALIBRATION = "calibration"
CONF_CHILD_LOCK = "child_lock"
CONF_PROTECT_OVERHEATING = "protect_overheating"
CONF_CALIBRATION_MODE = "calibration_mode"
CONF_HEAT_AUTO_SWAPPED = "heat_auto_swapped"
CONF_MODEL = "model"
CONF_HOMEMATICIP = "homematicip"
CONF_PRESETS = "presets"
CONF_INTEGRATION = "integration"
CONF_NO_SYSTEM_MODE_OFF = "no_off_system_mode"
CONF_TOLERANCE = "tolerance"
CONF_TARGET_TEMP_STEP = "target_temp_step"

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)

ATTR_STATE_WINDOW_OPEN = "window_open"
ATTR_STATE_CALL_FOR_HEAT = "call_for_heat"
ATTR_STATE_LAST_CHANGE = "last_change"
ATTR_STATE_SAVED_TEMPERATURE = "saved_temperature"
ATTR_STATE_PRESET_TEMPERATURE = "preset_temperature"
ATTR_VALVE_POSITION = "valve_position"
ATTR_STATE_HUMIDIY = "humidity"
ATTR_STATE_MAIN_MODE = "main_mode"
ATTR_STATE_HEATING_POWER = "heating_power"
ATTR_STATE_HEATING_STATS = "heating_stats"
ATTR_STATE_ERRORS = "errors"
ATTR_STATE_BATTERIES = "batteries"
# ECO mode logic removed; keep eco temperature for preset support

SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE = "restore_saved_target_temperature"
SERVICE_SET_TEMP_TARGET_TEMPERATURE = "set_temp_target_temperature"
# set_eco_mode service removed; ECO preset still supported via PRESET_ECO
SERVICE_RESET_HEATING_POWER = "reset_heating_power"
SERVICE_RESET_PID_LEARNINGS = "reset_pid_learnings"

BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA = vol.All(
    cv.has_at_least_one_key(ATTR_TEMPERATURE),
    make_entity_service_schema(
        {vol.Exclusive(ATTR_TEMPERATURE, "temperature"): vol.Coerce(float)}
    ),
)

# Optional schema for resetting PID learnings
BETTERTHERMOSTAT_RESET_PID_SCHEMA = make_entity_service_schema(
    {
        vol.Optional("apply_pid_defaults", default=False): cv.boolean,
        vol.Optional("defaults_kp"): vol.Coerce(float),
        vol.Optional("defaults_ki"): vol.Coerce(float),
        vol.Optional("defaults_kd"): vol.Coerce(float),
    }
)


class BetterThermostatEntityFeature(IntEnum):
    """Supported features of the climate entity."""

    TARGET_TEMPERATURE = 1
    TARGET_TEMPERATURE_RANGE = 2


class CalibrationType(StrEnum):
    """Calibration type."""

    TARGET_TEMP_BASED = "target_temp_based"
    LOCAL_BASED = "local_calibration_based"
    DIRECT_VALVE_BASED = "direct_valve_based"


class CalibrationMode(StrEnum):
    """Calibration mode."""

    DEFAULT = "default"
    AGGRESIVE_CALIBRATION = "fix_calibration"
    HEATING_POWER_CALIBRATION = "heating_power_calibration"
    NO_CALIBRATION = "no_calibration"
    MPC_CALIBRATION = "mpc_calibration"
    TPI_CALIBRATION = "tpi_calibration"
    PID_CALIBRATION = "pid_calibration"


# Heating power calibration constants
# These bounds represent realistic heating rates for residential heating systems
MIN_HEATING_POWER = 0.005  # °C/min - Very slow heating (poor insulation, cold climate)
MAX_HEATING_POWER = 0.2  # °C/min - Very fast heating (oversized system, small room)

# Valve position calculation constants for heating_power_valve_position()
VALVE_MIN_THRESHOLD_TEMP_DIFF = (
    0.3  # °C - Above this diff, enforce minimum valve opening
)
VALVE_MIN_OPENING_LARGE_DIFF = 0.15  # Minimum 15% valve opening when diff > 0.3°C
VALVE_MIN_BASE = 0.05  # Base minimum valve opening
VALVE_MIN_SMALL_DIFF_THRESHOLD = 0.1  # °C - Threshold for proportional minimum
VALVE_MIN_PROPORTIONAL_SLOPE = 0.5  # Slope for proportional minimum calculation
