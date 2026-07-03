"""Constants for Better Thermostat."""

from __future__ import annotations

from enum import IntEnum, StrEnum
import json
import logging
import os
from typing import Final, TypedDict

from homeassistant.components.climate.const import ClimateEntityFeature
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import make_entity_service_schema
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


DOMAIN: Final = "better_thermostat"

DEFAULT_NAME: Final = "Better Thermostat"


class _Manifest(TypedDict):
    version: str


VERSION: str = "master"
try:
    with open(
        f"{os.path.dirname(os.path.realpath(__file__))}/../manifest.json"
    ) as manifest_file:
        manifest: _Manifest = json.load(manifest_file)
        VERSION = manifest["version"]
except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
    _LOGGER.error("better_thermostat %s: could not read version from manifest file.", e)


CONF_HEATER: Final = "thermostat"
CONF_COOLER: Final = "cooler"
CONF_SENSOR: Final = "temperature_sensor"
CONF_HUMIDITY: Final = "humidity_sensor"
CONF_SENSOR_WINDOW: Final = "window_sensors"
CONF_TARGET_TEMP: Final = "target_temp"
CONF_WEATHER: Final = "weather"
CONF_OFF_TEMPERATURE: Final = "off_temperature"
CONF_WINDOW_TIMEOUT: Final = "window_off_delay"
CONF_WINDOW_TIMEOUT_AFTER: Final = "window_off_delay_after"
CONF_OUTDOOR_SENSOR: Final = "outdoor_sensor"
CONF_VALVE_MAINTENANCE: Final = "valve_maintenance"
CONF_MIN_TEMP: Final = "min_temp"
CONF_MAX_TEMP: Final = "max_temp"
CONF_PRECISION: Final = "precision"
CONF_CALIBRATION: Final = "calibration"
CONF_CHILD_LOCK: Final = "child_lock"
CONF_PROTECT_OVERHEATING: Final = "protect_overheating"
CONF_CALIBRATION_MODE: Final = "calibration_mode"
CONF_HEAT_AUTO_SWAPPED: Final = "heat_auto_swapped"
CONF_MODEL: Final = "model"
CONF_HOMEMATICIP: Final = "homematicip"
CONF_PRESETS: Final = "presets"
CONF_INTEGRATION: Final = "integration"
CONF_NO_SYSTEM_MODE_OFF: Final = "no_off_system_mode"
CONF_TOLERANCE: Final = "tolerance"
CONF_TARGET_TEMP_STEP: Final = "target_temp_step"

SUPPORT_FLAGS: Final = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)

ATTR_STATE_WINDOW_OPEN: Final = "window_open"
ATTR_STATE_CALL_FOR_HEAT: Final = "call_for_heat"
ATTR_STATE_LAST_CHANGE: Final = "last_change"
ATTR_STATE_SAVED_TEMPERATURE: Final = "saved_temperature"
ATTR_STATE_PRESET_TEMPERATURE: Final = "preset_temperature"
ATTR_VALVE_POSITION: Final = "valve_position"
ATTR_STATE_HUMIDIY: Final = "humidity"
ATTR_STATE_MAIN_MODE: Final = "main_mode"
ATTR_STATE_HEATING_POWER: Final = "heating_power"
ATTR_STATE_HEAT_LOSS: Final = "heat_loss"
ATTR_STATE_HEAT_LOSS_STATS: Final = "heat_loss_stats"
ATTR_STATE_HEATING_STATS: Final = "heating_stats"
ATTR_STATE_ERRORS: Final = "errors"
ATTR_STATE_BATTERIES: Final = "batteries"
ATTR_STATE_OFF_TEMPERATURE: Final = "off_temperature"
# ECO mode logic removed; keep eco temperature for preset support

# set_eco_mode and save/restore temperature services removed; ECO preset still supported via PRESET_ECO
SERVICE_RESET_HEATING_POWER: Final = "reset_heating_power"
SERVICE_RESET_PID_LEARNINGS: Final = "reset_pid_learnings"
SERVICE_RUN_VALVE_MAINTENANCE: Final = "run_valve_maintenance"

# Optional schema for resetting PID learnings
BETTERTHERMOSTAT_RESET_PID_SCHEMA: Final = make_entity_service_schema(
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


# Plausibility bounds for incoming temperature readings (Celsius).
# Values outside this window are treated as marker / garbage readings
# (for example, AVM Fritz!DECT exposes 126.5 / 127 °C when the thermostat
# is in OFF / ON mode) and rejected at the BT input boundary.
MIN_REASONABLE_TEMPERATURE = -50.0
MAX_REASONABLE_TEMPERATURE = 60.0

# Default temperature bounds / setpoint for the BT climate entity (Celsius),
# used until the underlying TRV reports its own min/max/target.
DEFAULT_MIN_TEMP: Final = 0.0
DEFAULT_MAX_TEMP: Final = 30.0
DEFAULT_TARGET_TEMP: Final = 5.0

# Heating power calibration constants
# These bounds represent realistic heating rates for residential heating systems
MIN_HEATING_POWER: Final = (
    0.005  # °C/min - Very slow heating (poor insulation, cold climate)
)
MAX_HEATING_POWER: Final = (
    0.2  # °C/min - Very fast heating (oversized system, small room)
)

# Heat loss estimation bounds (cooling rate) for residential buildings
MIN_HEAT_LOSS: Final = 0.001  # °C/min - very slow cooling
MAX_HEAT_LOSS: Final = 0.05  # °C/min - very fast cooling / high loss

# Valve position calculation constants for heating_power_valve_position()
VALVE_MIN_THRESHOLD_TEMP_DIFF: Final = (
    0.3  # °C - Above this diff, enforce minimum valve opening
)
VALVE_MIN_OPENING_LARGE_DIFF: Final = (
    0.15  # Minimum 15% valve opening when diff > 0.3°C
)
VALVE_MIN_BASE: Final = 0.05  # Base minimum valve opening
VALVE_MIN_SMALL_DIFF_THRESHOLD: Final = 0.1  # °C - Threshold for proportional minimum
VALVE_MIN_PROPORTIONAL_SLOPE: Final = 0.5  # Slope for proportional minimum calculation
