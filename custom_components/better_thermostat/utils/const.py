"""Constants for Better Thermostat."""

from __future__ import annotations

from enum import StrEnum
import json
import logging
import os
from typing import Final, TypedDict

from homeassistant.components.climate.const import ClimateEntityFeature
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.config_validation import make_entity_service_schema
import voluptuous as vol

# The preset enum lives with the calibration code, which imports nothing from
# Home Assistant, and is re-exported here next to the configuration key that
# stores it.
from .calibration.plant_preset import MpcV2PlantPreset  # noqa: F401

_LOGGER = logging.getLogger(__name__)


DOMAIN: Final = "better_thermostat"

DEFAULT_NAME: Final = "Better Thermostat"

# ``hass.data`` key holding, per entry and platform, the thermostat name the
# entity ids were last built from. It outlives the reload it describes and is
# dropped when the entry is removed.
NORMALIZED_ID_NAMES: Final = f"{DOMAIN}_normalized_id_names"


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


CONF_THERMOSTAT: Final = "thermostat"
CONF_COOLER: Final = "cooler"
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
CONF_WINDOW_SENSORS: Final = "window_sensors"
CONF_DOOR_SENSORS: Final = "door_sensors"
CONF_TARGET_TEMP: Final = "target_temp"
CONF_WEATHER: Final = "weather"
CONF_OFF_TEMPERATURE: Final = "off_temperature"
CONF_WINDOW_OFF_DELAY: Final = "window_off_delay"
CONF_WINDOW_OFF_DELAY_AFTER: Final = "window_off_delay_after"
CONF_DOOR_OFF_DELAY: Final = "door_off_delay"
CONF_DOOR_OFF_DELAY_AFTER: Final = "door_off_delay_after"
CONF_OUTDOOR_SENSOR: Final = "outdoor_sensor"
CONF_VALVE_MAINTENANCE: Final = "valve_maintenance"
CONF_MIN_TEMP: Final = "min_temp"
CONF_MAX_TEMP: Final = "max_temp"
CONF_PRECISION: Final = "precision"
CONF_CALIBRATION: Final = "calibration"
CONF_CHILD_LOCK: Final = "child_lock"
CONF_PROTECT_OVERHEATING: Final = "protect_overheating"
CONF_CALIBRATION_MODE: Final = "calibration_mode"
CONF_MPC_V2_PLANT_PRESET: Final = "mpc_v2_plant_preset"
CONF_HEAT_AUTO_SWAPPED: Final = "heat_auto_swapped"
CONF_MODEL: Final = "model"
CONF_HOMEMATICIP: Final = "homematicip"
CONF_PRESETS: Final = "presets"
CONF_INTEGRATION: Final = "integration"
CONF_NO_OFF_SYSTEM_MODE: Final = "no_off_system_mode"
CONF_TOLERANCE: Final = "tolerance"
CONF_TARGET_TEMP_MIN: Final = "target_temp_min"
CONF_TARGET_TEMP_MAX: Final = "target_temp_max"
# Stored value of a target temperature bound that is left to the controlled
# entities instead of being pinned to a degree.
TARGET_TEMP_BOUND_AUTO: Final = "-1.0"
CONF_TARGET_TEMP_STEP: Final = "target_temp_step"

# Model string a TRV carries when the device registry has nothing that
# identifies it. No quirk module answers for it, so a TRV on this model is
# driven by the default quirks, the same as one whose model is unknown.
GENERIC_MODEL: Final = "generic"

SUPPORT_FLAGS: Final = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.TURN_ON
)

ATTR_STATE_WINDOW_OPEN: Final = "window_open"
ATTR_STATE_DOOR_OPEN: Final = "door_open"
ATTR_STATE_CALL_FOR_HEAT: Final = "call_for_heat"
ATTR_STATE_LAST_CHANGE: Final = "last_change"
ATTR_STATE_PRESET_TEMPERATURE: Final = "preset_temperature"
ATTR_STATE_PRESET_COOL_TEMPERATURE: Final = "preset_cool_temperature"
ATTR_STATE_PRESET_COOL_TEMPERATURES: Final = "preset_cool_temperatures"
ATTR_STATE_PRESET_HEAT_TEMPERATURES: Final = "preset_heat_temperatures"
ATTR_STATE_ROOM_TEMPERATURE_FILTERED: Final = "room_temperature_filtered"
ATTR_STATE_DEGRADED_FOR_SECONDS: Final = "degraded_for_seconds"
ATTR_STATE_TEMPERATURE_SLOPE: Final = "temperature_slope_kelvin_per_min"
ATTR_VALVE_POSITION: Final = "valve_position"
ATTR_STATE_HUMIDITY: Final = "humidity"
ATTR_STATE_MAIN_MODE: Final = "main_mode"
ATTR_STATE_HEATING_POWER: Final = "heating_power"
ATTR_STATE_HEAT_LOSS: Final = "heat_loss"
ATTR_STATE_HEAT_LOSS_STATS: Final = "heat_loss_stats"
ATTR_STATE_HEATING_STATS: Final = "heating_stats"
ATTR_STATE_ERRORS: Final = "errors"
ATTR_STATE_BATTERIES: Final = "batteries"
ATTR_STATE_OFF_TEMPERATURE: Final = "off_temperature"
ATTR_HEATING_POWER_NORMALIZED: Final = "heating_power_normalized"
ATTR_PID_ERROR: Final = "pid_error_kelvin"
ATTR_PID_MEASUREMENT_FILTERED: Final = "pid_measurement_filtered"
ATTR_PID_MEASUREMENT_SLOPE: Final = "pid_measurement_slope_kelvin_per_min"
ATTR_PID_DT: Final = "pid_dt_seconds"
ATTR_MPC_V2_ROOM_TEMPERATURE: Final = "mpc_v2_room_temperature_estimate"
ATTR_MPC_V2_RADIATOR_TEMPERATURE: Final = "mpc_v2_radiator_temperature_estimate"
ATTR_MPC_V2_COUPLING: Final = "mpc_v2_radiator_room_coupling"
ATTR_MPC_V2_DISTURBANCE: Final = "mpc_v2_disturbance_kelvin_per_min"
ATTR_MPC_V2_TAU_ROOM: Final = "mpc_v2_tau_room_minutes"
ATTR_MPC_V2_GROUP_VALVE: Final = "mpc_v2_group_valve_percent"
ATTR_MPC_V2_REID_TAU_ROOM: Final = "mpc_v2_reid_tau_room_minutes"

# DEPRECATED, remove in 3.0: state attributes under the names 1.9 published,
# keyed by the current name. The entity publishes each value under
# both names, because templates read the old ones and 1.9 restores from them
# after a rollback, and a restart reads the current name first and the old one
# after it. Removing an entry here removes its old name everywhere.
DEPRECATED_STATE_ATTRIBUTES: Final[dict[str, str]] = {
    ATTR_STATE_PRESET_COOL_TEMPERATURE: "bt_preset_cool_temperature",
    ATTR_STATE_PRESET_COOL_TEMPERATURES: "bt_preset_cool_temperatures",
    ATTR_STATE_PRESET_HEAT_TEMPERATURES: "bt_preset_heat_temperatures",
    ATTR_STATE_ROOM_TEMPERATURE_FILTERED: "external_temp_ema",
    ATTR_STATE_TEMPERATURE_SLOPE: "temp_slope_K_min",
    ATTR_PID_ERROR: "pid_e_K",
    ATTR_PID_MEASUREMENT_FILTERED: "pid_meas_smooth_C",
    ATTR_PID_MEASUREMENT_SLOPE: "pid_d_meas_K_per_min",
    ATTR_PID_DT: "pid_dt_s",
    ATTR_HEATING_POWER_NORMALIZED: "heating_power_norm",
    ATTR_MPC_V2_ROOM_TEMPERATURE: "mpc_v2_T_room_hat",
    ATTR_MPC_V2_RADIATOR_TEMPERATURE: "mpc_v2_T_rad_hat",
    ATTR_MPC_V2_COUPLING: "mpc_v2_coupling_rad_room",
    ATTR_MPC_V2_DISTURBANCE: "mpc_v2_D_hat_K_per_min",
    ATTR_MPC_V2_TAU_ROOM: "mpc_v2_tau_room_min",
    ATTR_MPC_V2_GROUP_VALVE: "mpc_v2_group_valve_pct",
}

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


class CalibrationOutput(StrEnum):
    """What BT writes to a TRV to calibrate it: setpoint, offset or valve."""

    TARGET_TEMP_BASED = "target_temp_based"
    LOCAL_BASED = "local_calibration_based"
    DIRECT_VALVE_BASED = "direct_valve_based"


class CalibrationMode(StrEnum):
    """The algorithm that computes the calibration."""

    DEFAULT = "default"
    AGGRESSIVE_CALIBRATION = "fix_calibration"
    HEATING_POWER_CALIBRATION = "heating_power_calibration"
    NO_CALIBRATION = "no_calibration"
    MPC_CALIBRATION = "mpc_calibration"
    MPC_V2_CALIBRATION = "mpc_v2_calibration"
    TPI_CALIBRATION = "tpi_calibration"
    PID_CALIBRATION = "pid_calibration"


# The calibration mode a TRV runs in when its config carries none. Read by
# the config flow (form default and normalisation) and by every runtime
# fallback, so a stored config without the key behaves like a fresh one.
DEFAULT_CALIBRATION_MODE: Final = CalibrationMode.HEATING_POWER_CALIBRATION


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
