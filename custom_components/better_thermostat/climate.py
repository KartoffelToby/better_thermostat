"""Better Thermostat."""

from abc import ABC
import asyncio
from collections import deque
from datetime import datetime, timedelta
import json
import logging
from random import randint
from statistics import mean
from time import monotonic
from typing import Any

# Home Assistant imports
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.group.util import reduce_attribute
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, CoreState, ServiceCall, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.helpers.dispatcher import dispatcher_send

# preferred for HA time handling (UTC aware)
from homeassistant.util import dt as dt_util

# Local imports
from .adapters.delegate import (
    get_current_offset,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    load_adapter,
    set_hvac_mode as adapter_set_hvac_mode,
    set_temperature as adapter_set_temperature,
)
from .events.cooler import trigger_cooler_change
from .events.temperature import trigger_temperature_change
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue
from .model_fixes.model_quirks import inital_tweak, load_model_quirks
from .utils.calibration.mpc import export_mpc_state_map, import_mpc_state_map
from .utils.calibration.pid import (
    export_pid_states as pid_export_states,
    import_pid_states as pid_import_states,
    reset_pid_state as pid_reset_state,
)
from .utils.calibration.tpi import export_tpi_state_map, import_tpi_state_map
from .utils.const import (
    ATTR_STATE_BATTERIES,
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_ERRORS,
    ATTR_STATE_HEAT_LOSS,
    ATTR_STATE_HEAT_LOSS_STATS,
    ATTR_STATE_HEATING_POWER,
    ATTR_STATE_HUMIDIY,
    ATTR_STATE_LAST_CHANGE,
    ATTR_STATE_MAIN_MODE,
    ATTR_STATE_OFF_TEMPERATURE,
    ATTR_STATE_PRESET_TEMPERATURE,
    ATTR_STATE_SAVED_TEMPERATURE,
    ATTR_STATE_WINDOW_OPEN,
    BETTERTHERMOSTAT_RESET_PID_SCHEMA,
    BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
    CONF_COOLER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_PRESETS,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_TARGET_TEMP_STEP,
    CONF_TOLERANCE,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
    MIN_HEAT_LOSS,
    MIN_HEATING_POWER,
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESET_PID_LEARNINGS,
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE,
    SERVICE_SET_TEMP_TARGET_TEMPERATURE,
    SUPPORT_FLAGS,
    VERSION,
    CalibrationMode,
    CalibrationType,
)
from .utils.controlling import control_queue, control_trv
from .utils.helpers import (
    convert_to_float,
    find_battery_entity,
    get_device_model,
    get_hvac_bt_mode,
    normalize_hvac_mode,
)
from .utils.watcher import (
    check_and_update_degraded_mode,
    check_critical_entities,
    is_entity_available,
)
from .utils.weather import check_ambient_air_temperature, check_weather

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"

# Default temperature when no sensor data is available (last resort fallback)
DEFAULT_FALLBACK_TEMPERATURE = 20.0

# Signal für dynamische Entity-Updates
SIGNAL_BT_CONFIG_CHANGED = "bt_config_changed_{}"


class ContinueLoop(Exception):
    """Continue loop exception."""

    pass


@callback
def async_set_temperature_service_validate(service_call: ServiceCall) -> ServiceCall:
    """Validate temperature inputs for set_temperature service."""
    if ATTR_TEMPERATURE in service_call.data:
        temp = service_call.data[ATTR_TEMPERATURE]
        if not isinstance(temp, (int, float)):
            raise ValueError(f"Invalid temperature value {temp}, must be numeric")

    if ATTR_TARGET_TEMP_HIGH in service_call.data:
        temp_high = service_call.data[ATTR_TARGET_TEMP_HIGH]
        if not isinstance(temp_high, (int, float)):
            raise ValueError(
                f"Invalid target high temperature value {temp_high}, must be numeric"
            )

    if ATTR_TARGET_TEMP_LOW in service_call.data:
        temp_low = service_call.data[ATTR_TARGET_TEMP_LOW]
        if not isinstance(temp_low, (int, float)):
            raise ValueError(
                f"Invalid target low temperature value {temp_low}, must be numeric"
            )

    return service_call


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Better Thermostat platform."""
    _LOGGER.debug("better_thermostat: async_setup_platform called (deprecated no-op)")


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up Better Thermostat climate entity for a config entry."""
    _LOGGER.debug(
        "better_thermostat %s: async_setup_entry start (entry_id=%s)",
        entry.data.get(CONF_NAME),
        entry.entry_id,
    )

    platform = entity_platform.async_get_current_platform()
    # Register entity services (validator done manually inside method)
    platform.async_register_entity_service(
        SERVICE_SET_TEMP_TARGET_TEMPERATURE,
        BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
        "set_temp_temperature",
    )
    platform.async_register_entity_service(
        SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE, {}, "restore_temp_temperature"
    )
    platform.async_register_entity_service(
        SERVICE_RESET_HEATING_POWER, {}, "reset_heating_power"
    )
    platform.async_register_entity_service(
        "run_valve_maintenance", {}, "run_valve_maintenance_service"
    )
    platform.async_register_entity_service(
        SERVICE_RESET_PID_LEARNINGS,
        BETTERTHERMOSTAT_RESET_PID_SCHEMA,
        "reset_pid_learnings_service",
    )

    bt_entity = BetterThermostat(
        entry.data.get(CONF_NAME),
        entry.data.get(CONF_HEATER),
        entry.data.get(CONF_SENSOR),
        entry.data.get(CONF_HUMIDITY, None),
        entry.data.get(CONF_SENSOR_WINDOW, None),
        entry.data.get(CONF_WINDOW_TIMEOUT, None),
        entry.data.get(CONF_WINDOW_TIMEOUT_AFTER, None),
        entry.data.get(CONF_WEATHER, None),
        entry.data.get(CONF_OUTDOOR_SENSOR, None),
        entry.data.get(CONF_OFF_TEMPERATURE, None),
        entry.data.get(CONF_TOLERANCE, 0.0),
        entry.data.get(CONF_TARGET_TEMP_STEP, "0.0"),
        entry.data.get(CONF_MODEL, None),
        entry.data.get(CONF_COOLER, None),
        entry.data.get(CONF_PRESETS, None),
        hass.config.units.temperature_unit,
        entry.entry_id,
        device_class="better_thermostat",
        state_class="better_thermostat_state",
    )
    hass.data[DOMAIN][entry.entry_id]["climate"] = bt_entity
    async_add_entities([bt_entity])
    _LOGGER.debug(
        "better_thermostat %s: async_setup_entry finished creating entity",
        entry.data.get(CONF_NAME),
    )


class BetterThermostat(ClimateEntity, RestoreEntity, ABC):
    """Representation of a Better Thermostat device."""

    _attr_has_entity_name = True
    _attr_name = None
    _enable_turn_on_off_backwards_compatibility = False

    async def set_temp_temperature(self, temperature):
        """Set temporary target temperature."""
        self.bt_update_lock = True
        try:
            if self._saved_temperature is None:
                self._saved_temperature = self.bt_target_temp
                self.bt_target_temp = convert_to_float(
                    temperature, self.device_name, "service.set_temp_temperature()"
                )
                self.async_write_ha_state()
                if getattr(self, "in_maintenance", False):
                    self._control_needed_after_maintenance = True
                    return
                await self.control_queue_task.put(self)
            else:
                self.bt_target_temp = convert_to_float(
                    temperature, self.device_name, "service.set_temp_temperature()"
                )
                self.async_write_ha_state()
                if getattr(self, "in_maintenance", False):
                    self._control_needed_after_maintenance = True
                    return
                await self.control_queue_task.put(self)
        finally:
            self.bt_update_lock = False

    async def restore_temp_temperature(self):
        """Restore the previously saved target temperature."""
        self.bt_update_lock = True
        try:
            if self._saved_temperature is not None:
                self.bt_target_temp = convert_to_float(
                    self._saved_temperature,
                    self.device_name,
                    "service.restore_temp_temperature()",
                )
                self._saved_temperature = None
                self.async_write_ha_state()
                if getattr(self, "in_maintenance", False):
                    self._control_needed_after_maintenance = True
                    return
                await self.control_queue_task.put(self)
        finally:
            self.bt_update_lock = False

    # ECO mode removed; set_eco_mode service and logic deleted.

    async def reset_heating_power(self):
        """Reset heating power to default value."""
        self.heating_power = 0.01
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.device_name,
            "manufacturer": "Better Thermostat",
            "model": self.model,
            "sw_version": VERSION,
        }

    def __init__(
        self,
        name,
        heater_entity_id,
        sensor_entity_id,
        humidity_sensor_entity_id,
        window_id,
        window_delay,
        window_delay_after,
        weather_entity,
        outdoor_sensor,
        off_temperature,
        tolerance,
        target_temp_step,
        model,
        cooler_entity_id,
        enabled_presets,
        unit,
        unique_id,
        device_class,
        state_class,
    ):
        """Initialize the thermostat.

        Parameters
        ----------
        TODO
        """
        self.device_name = name
        self.model = model
        self.real_trvs = {}
        self.entity_ids = []
        self.all_trvs = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.humidity_sensor_entity_id = humidity_sensor_entity_id
        self.cooler_entity_id = cooler_entity_id
        self.window_id = window_id or None
        self.window_delay = window_delay or 0
        self.window_delay_after = window_delay_after or 0
        self.weather_entity = weather_entity or None
        self.outdoor_sensor = outdoor_sensor or None
        # Robust off temperature parsing: preserve 0.0 and ignore invalid strings
        self.off_temperature = None
        if off_temperature not in (None, "", "None"):  # allow numeric 0
            try:
                parsed_off = float(off_temperature)
                # Accept any float (including 0.0); reject extreme nonsense
                if -100.0 < parsed_off < 150.0:
                    self.off_temperature = parsed_off
                else:
                    _LOGGER.warning(
                        "better_thermostat %s: off_temperature %.2f outside plausible range, ignoring",
                        self.device_name,
                        parsed_off,
                    )
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "better_thermostat %s: invalid off_temperature '%s', ignoring",
                    self.device_name,
                    off_temperature,
                )

        # Robust tolerance parsing & sanitizing
        try:
            self.tolerance = float(tolerance) if tolerance is not None else 0.0
        except (TypeError, ValueError):
            _LOGGER.warning(
                "better_thermostat %s: invalid tolerance '%s', falling back to 0.0",
                self.device_name,
                tolerance,
            )
            self.tolerance = 0.0
        if self.tolerance < 0:
            _LOGGER.warning(
                "better_thermostat %s: negative tolerance '%s' adjusted to 0.0",
                self.device_name,
                self.tolerance,
            )
            self.tolerance = 0.0
        if self.tolerance > 10:
            _LOGGER.warning(
                "better_thermostat %s: unusually high tolerance '%s' (>10) may cause sluggish response",
                self.device_name,
                self.tolerance,
            )
        self._unique_id = unique_id
        self._unit = unit
        self._device_class = device_class
        self._state_class = state_class
        self._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
        self._preset_mode = PRESET_NONE
        self.map_on_hvac_mode = HVACMode.HEAT
        self.next_valve_maintenance = datetime.now() + timedelta(
            hours=randint(1, 24 * 5)
        )
        self.cur_temp = None
        self._current_humidity = 0
        self.window_open = None
        self.bt_target_temp_step = float(target_temp_step) or 0.0
        self.bt_min_temp = 0
        self.bt_max_temp = 30
        self.bt_target_temp = 5.0
        self.bt_target_cooltemp = None
        self._support_flags = SUPPORT_FLAGS | ClimateEntityFeature.PRESET_MODE
        self.bt_hvac_mode = None
        # Track min/max encountered target temps (initialize to default span)
        self.min_target_temp = 18.0
        self.max_target_temp = 21.0
        self.closed_window_triggered = False
        self.call_for_heat = True
        self.ignore_states = False
        self.last_dampening_timestamp = None
        self.version = VERSION
        self.last_change = datetime.now() - timedelta(hours=2)
        self.last_external_sensor_change = datetime.now() - timedelta(hours=2)
        self.last_internal_sensor_change = datetime.now() - timedelta(hours=2)
        self._temp_lock = asyncio.Lock()
        self.bt_update_lock = False
        self.startup_running = True
        self._saved_temperature = None
        # ECO mode removed; preserved eco preset via PRESET_ECO
        self._preset_temperature = (
            None  # Temperature saved before entering any preset mode
        )
        self._enabled_presets = enabled_presets
        if not self._enabled_presets:
            self._enabled_presets = [
                PRESET_AWAY,
                PRESET_BOOST,
                PRESET_SLEEP,
                PRESET_COMFORT,
                PRESET_ECO,
                PRESET_ACTIVITY,
                PRESET_HOME,
            ]

        self._preset_temperatures = {
            PRESET_NONE: 20.0,
            PRESET_AWAY: 16.0,
            PRESET_BOOST: 24.0,
            PRESET_COMFORT: 21.0,
            PRESET_ECO: 19.0,
            PRESET_HOME: 20.0,
            PRESET_SLEEP: 18.0,
            PRESET_ACTIVITY: 22.0,
        }
        # Keep a copy of original configured preset temperatures to detect user customization
        self._original_preset_temperatures = self._preset_temperatures.copy()
        # Config entry id (same as unique id passed in) used for durable persistence beyond RestoreEntity
        self._config_entry_id = self._unique_id
        self.last_avg_outdoor_temp = None
        self.last_main_hvac_mode = None
        self.last_window_state = None
        self._last_call_for_heat = None
        self._available = False
        self.context = None
        self.attr_hvac_action = None
        self.old_attr_hvac_action = None
        self._tolerance_last_action = HVACAction.IDLE
        self._tolerance_hold_active = False
        self.heating_start_temp = None
        self.heating_start_timestamp = None
        self.heating_end_temp = None
        self.heating_end_timestamp = None
        # Heat loss tracking (idle cooling rate)
        self.loss_start_temp = None
        self.loss_start_timestamp = None
        self.loss_end_temp = None
        self.loss_end_timestamp = None
        self.heat_loss_rate = 0.01
        self.last_heat_loss_stats = deque(maxlen=10)
        self.loss_cycles = deque(maxlen=50)
        self._loss_last_action = None
        self._async_unsub_state_changed = None
        self.all_entities = []
        self.devices_states = {}
        self.devices_errors = []
        # Degraded mode: thermostat continues operating with some sensors unavailable
        self.degraded_mode = False
        self.unavailable_sensors = []
        self.control_queue_task = asyncio.Queue(maxsize=1)
        if self.window_id is not None:
            self.window_queue_task = asyncio.Queue(maxsize=1)
        self._control_task = asyncio.create_task(control_queue(self))
        self._window_task = None
        if self.window_id is not None:
            self._window_task = asyncio.create_task(window_queue(self))
        self.heating_power = 0.01
        # Short bounded history of recent heating power evaluations
        self.last_heating_power_stats = deque(maxlen=10)
        self.is_removed = False
        # Valve maintenance control
        self.in_maintenance = False
        # If control actions are requested during valve maintenance, defer them and
        # trigger one control cycle once maintenance finishes.
        self._control_needed_after_maintenance = False
        # Balance / Hydraulic: temperature trend (K/min)
        self.temp_slope = None
        self._slope_last_temp = None
        self._slope_last_ts = None
        # External temperature filter (anti-jitter for controllers like MPC)
        # 900s = 15min, 1800s = 30min
        self.external_temp_ema_tau_s = 300.0
        self.external_temp_ema = None
        self._external_temp_ema_ts = None
        self.cur_temp_filtered = None
        # Persistence for balance (hydraulic) states
        self._pid_store = None
        self._pid_save_scheduled = False
        # MPC adaptive state persistence
        self._mpc_store = None
        self._mpc_save_scheduled = False
        # TPI adaptive state persistence
        self._tpi_store = None
        self._tpi_save_scheduled = False
        # Thermal stats persistence (heating_power / heat_loss)
        self._thermal_store = None
        self._thermal_save_scheduled = False

        self.last_known_external_temp = None
        self._slope_periodic_last_ts = None

        # Anti-flicker state
        self.flicker_unignore_cancel = None
        self.flicker_candidate = None
        self.plateau_timer_cancel = None
        self.last_change_direction = 0
        self.prev_stable_temp = None
        self.accum_delta = 0.0
        self.accum_dir = 0
        self.accum_since = datetime.now()
        self.pending_temp = None
        self.pending_since = None

    async def async_added_to_hass(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        if isinstance(self.all_trvs, str):
            return _LOGGER.error(
                "You updated from version before 1.0.0-Beta36 of the Better Thermostat integration, " \
                "you need to remove the BT devices (integration) and add it again."
            )

        if self.cooler_entity_id is not None:
            self._hvac_list.remove(HVACMode.HEAT)
            self._hvac_list.append(HVACMode.HEAT_COOL)
            self.map_on_hvac_mode = HVACMode.HEAT_COOL

        self.entity_ids = [
            entity for trv in self.all_trvs if (entity := trv["trv"]) is not None
        ]

        for trv in self.all_trvs:
            _calibration = 1
            _advanced = trv.get("advanced", {})
            _calibration_type = _advanced.get("calibration")
            if _calibration_type == CalibrationType.TARGET_TEMP_BASED:
                _calibration = 0
            if _calibration_type == CalibrationType.DIRECT_VALVE_BASED:
                _calibration = 2
            if _calibration_type == CalibrationType.LOCAL_BASED:
                _calibration = 3
            _adapter = await load_adapter(self, trv["integration"], trv["trv"])
            # Resolve/refresh model dynamically at startup to ensure correct quirks
            resolved_model = trv.get("model")
            try:
                # prefers state model_id when present
                detected_model = await get_device_model(self, trv["trv"])
                if (
                    isinstance(detected_model, str)
                    and detected_model
                    and detected_model != resolved_model
                ):
                    _LOGGER.info(
                        "better_thermostat %s: detected model '%s' for %s (was '%s' in config), using detected model",
                        self.device_name,
                        detected_model,
                        trv["trv"],
                        resolved_model,
                    )
                    resolved_model = detected_model
            except (AttributeError, TypeError) as e:
                _LOGGER.debug(
                    "better_thermostat %s: get_device_model(%s) failed: %s",
                    self.device_name,
                    trv.get("trv"),
                    e,
                )
            _LOGGER.debug(
                "better_thermostat %s: loading model quirks: model='%s' trv='%s'",
                self.device_name,
                resolved_model,
                trv.get("trv"),
            )
            _model_quirks = await load_model_quirks(self, resolved_model, trv["trv"])
            try:
                mod_name = getattr(_model_quirks, "__name__", str(_model_quirks))
                _LOGGER.debug(
                    "better_thermostat %s: loaded model quirks module '%s' for model '%s' (trv %s)",
                    self.device_name,
                    mod_name,
                    resolved_model,
                    trv.get("trv"),
                )
            except (AttributeError, TypeError) as e:
                _LOGGER.debug(
                    "better_thermostat %s: could not determine quirks module name for model '%s' (trv %s): %s",
                    self.device_name,
                    resolved_model,
                    trv.get("trv"),
                    e,
                )
            self.real_trvs[trv["trv"]] = {
                "calibration": _calibration,
                "integration": trv["integration"],
                "adapter": _adapter,
                "model_quirks": _model_quirks,
                "model": resolved_model,
                "advanced": _advanced,
                "ignore_trv_states": False,
                "valve_position": None,
                "valve_position_entity": None,
                "valve_position_writable": None,
                "max_temp": None,
                "min_temp": None,
                "target_temp_step": None,
                "temperature": None,
                "current_temperature": None,
                "hvac_modes": None,
                "hvac_mode": None,
                "local_temperature_calibration_entity": None,
                "local_calibration_min": None,
                "local_calibration_max": None,
                "calibration_received": True,
                "target_temp_received": True,
                "system_mode_received": True,
                "last_temperature": None,
                "last_valve_position": None,
                "last_hvac_mode": None,
                "last_current_temperature": None,
                "last_calibration": None,
            }

        def on_remove():
            self.is_removed = True
            if self._mpc_store is not None:
                try:
                    self.hass.async_create_task(self._save_mpc_states())
                except RuntimeError:
                    pass

        self.async_on_remove(on_remove)

        await super().async_added_to_hass()

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.device_name
        )

        # Initialize persistent storage for balance states and attempt to load
        try:
            self._pid_store = Store(self.hass, 1, f"{DOMAIN}_pid_states")
            await self._load_pid_state()
        except (FileNotFoundError, PermissionError, RuntimeError) as e:
            _LOGGER.debug(
                "better_thermostat %s: PID storage init/load failed: %s",
                self.device_name,
                e,
            )

        # Initialize persistent storage for MPC adaptive state
        try:
            self._mpc_store = Store(self.hass, 1, f"{DOMAIN}_mpc_states")
            await self._load_mpc_states()
        except (FileNotFoundError, PermissionError, RuntimeError) as e:
            _LOGGER.debug(
                "better_thermostat %s: MPC storage init/load failed: %s",
                self.device_name,
                e,
            )

        # Initialize persistent storage for TPI adaptive state
        try:
            self._tpi_store = Store(self.hass, 1, f"{DOMAIN}_tpi_states")
            await self._load_tpi_states()
        except (FileNotFoundError, PermissionError, RuntimeError) as e:
            _LOGGER.debug(
                "better_thermostat %s: TPI storage init/load failed: %s",
                self.device_name,
                e,
            )

        # Initialize persistent storage for thermal stats (heating_power / heat_loss)
        try:
            self._thermal_store = Store(self.hass, 1, f"{DOMAIN}_thermal_stats")
            await self._load_thermal_stats()
        except (FileNotFoundError, PermissionError, RuntimeError) as e:
            _LOGGER.debug(
                "better_thermostat %s: thermal stats storage init/load failed: %s",
                self.device_name,
                e,
            )

        @callback
        def _async_startup(*_):
            """Init on startup.

            Parameters
            ----------
            _ :
                    All parameters are piped.
            """
            self.context = Context()
            loop = asyncio.get_event_loop()
            loop.create_task(self.startup())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def _trigger_check_weather(self, event=None):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        await check_weather(self)
        if self._last_call_for_heat != self.call_for_heat:
            self._last_call_for_heat = self.call_for_heat
            await self.async_update_ha_state(force_refresh=True)
            self.async_write_ha_state()
            if event is not None:
                await self.control_queue_task.put(self)

    async def _trigger_time(self, event=None):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        if getattr(self, "in_maintenance", False):
            _LOGGER.debug(
                "better_thermostat %s: periodic tick skipped (valve maintenance running)",
                self.device_name,
            )
            return
        _LOGGER.debug(
            "better_thermostat %s: get last avg outdoor temps...", self.device_name
        )
        await check_ambient_air_temperature(self)
        self.async_write_ha_state()
        if event is not None:
            await self.control_queue_task.put(self)

    async def _trigger_temperature_change(self, event):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self.hass.async_create_task(trigger_temperature_change(self, event))

    async def _external_temperature_keepalive(self, event=None):
        """Re-send the external temperature regularly to the TRVs.

        Many devices expect an update at least every ~30 minutes.
        """
        try:
            cur = self.cur_temp
            if cur is None:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive skipped (cur_temp is None)",
                    self.device_name,
                )
                return

            # Verwende die bekannten TRV-Entity-IDs (Keys in real_trvs)
            trv_ids = list(self.real_trvs.keys())
            # Fallback (sollte i.d.R. nicht benötigt werden)
            if not trv_ids and hasattr(self, "entity_ids"):
                trv_ids = list(self.entity_ids or [])
            if not trv_ids and hasattr(self, "heater_entity_id"):
                trv_ids = [self.heater_entity_id]
            if not trv_ids:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive: no TRVs found",
                    self.device_name,
                )
                return
            else:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive: %d TRV(s) found",
                    self.device_name,
                    len(trv_ids),
                )

            for trv_id in trv_ids:
                try:
                    quirks = (
                        self.real_trvs.get(trv_id, {}).get("model_quirks")
                        if hasattr(self, "real_trvs")
                        else None
                    )
                    if quirks and hasattr(quirks, "maybe_set_external_temperature"):
                        ok = await quirks.maybe_set_external_temperature(
                            self, trv_id, cur
                        )
                        _LOGGER.debug(
                            "better_thermostat %s: external_temperature keepalive sent to %s (ok=%s, value=%s)",
                            self.device_name,
                            trv_id,
                            ok,
                            cur,
                        )
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: no quirks with maybe_set_external_temperature for %s",
                            self.device_name,
                            trv_id,
                        )
                except (OSError, RuntimeError, AttributeError, TypeError):
                    _LOGGER.debug(
                        "better_thermostat %s: external_temperature keepalive write failed for %s (non critical)",
                        self.device_name,
                        trv_id,
                    )
        except (OSError, RuntimeError, AttributeError, TypeError):
            _LOGGER.debug(
                "better_thermostat %s: external_temperature keepalive encountered an error",
                self.device_name,
            )

    async def _trigger_humidity_change(self, event):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        # Only update humidity if sensor is available
        if is_entity_available(self.hass, self.humidity_sensor_entity_id):
            self._current_humidity = convert_to_float(
                str(self.hass.states.get(self.humidity_sensor_entity_id).state),
                self.device_name,
                "humidity_update",
            )
        self.async_write_ha_state()

    async def _trigger_trv_change(self, event):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        if getattr(self, "in_maintenance", False):
            _LOGGER.debug(
                "better_thermostat %s: TRV change skipped (valve maintenance running)",
                self.device_name,
            )
            return
        self.async_set_context(event.context)
        if self._async_unsub_state_changed is None:
            return

        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_trv_change(self, event))

    async def _trigger_window_change(self, event):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        # Only process window changes if window sensor is available
        if is_entity_available(self.hass, self.window_id):
            self.hass.async_create_task(trigger_window_change(self, event))

    async def _tigger_cooler_change(self, event):
        _check = await check_critical_entities(self)
        if _check is False:
            return
        await check_and_update_degraded_mode(self)
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_cooler_change(self, event))

    def _set_trv_calibration_defaults(self, trv):
        """Set default calibration values for TRV."""
        if self.real_trvs[trv].get("last_calibration") is None:
            self.real_trvs[trv]["last_calibration"] = 0
        if self.real_trvs[trv].get("local_calibration_min") is None:
            self.real_trvs[trv]["local_calibration_min"] = -7
        if self.real_trvs[trv].get("local_calibration_max") is None:
            self.real_trvs[trv]["local_calibration_max"] = 7
        if self.real_trvs[trv].get("local_calibration_step") is None:
            self.real_trvs[trv]["local_calibration_step"] = 0.5

    async def startup(self):
        """Run when entity about to be added."""
        while self.startup_running:
            _LOGGER.info(
                "better_thermostat %s: Starting version %s. Waiting for entity to be ready...",
                self.device_name,
                self.version,
            )

            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if sensor_state is None or sensor_state.state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                _LOGGER.info(
                    "better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
                    self.device_name,
                    self.sensor_entity_id,
                )
                await asyncio.sleep(20)
                continue

            try:
                for trv in self.real_trvs.keys():
                    trv_state = self.hass.states.get(trv)
                    if trv_state is None:
                        _LOGGER.info(
                            "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                            self.device_name,
                            trv,
                        )
                        await asyncio.sleep(20)
                        raise ContinueLoop
                    if trv_state is not None:
                        if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                            _LOGGER.info(
                                "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                                self.device_name,
                                trv,
                            )
                            await asyncio.sleep(20)
                            raise ContinueLoop
            except ContinueLoop:
                continue

            # Optional sensors: log warning but don't block startup (degraded mode)
            if self.window_id is not None:
                _win_state = self.hass.states.get(self.window_id)
                if _win_state is None or _win_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.warning(
                        "better_thermostat %s: Window sensor '%s' unavailable at startup. "
                        "Continuing in degraded mode (assuming window closed).",
                        self.device_name,
                        self.window_id,
                    )
                    self.unavailable_sensors.append(self.window_id)

            if self.cooler_entity_id is not None:
                _cool_state = self.hass.states.get(self.cooler_entity_id)
                if _cool_state is None or _cool_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.warning(
                        "better_thermostat %s: Cooler entity '%s' unavailable at startup. "
                        "Continuing without cooling support.",
                        self.device_name,
                        self.cooler_entity_id,
                    )
                    self.unavailable_sensors.append(self.cooler_entity_id)

            if self.humidity_sensor_entity_id is not None:
                humidity_state = self.hass.states.get(self.humidity_sensor_entity_id)
                if humidity_state is None or humidity_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.warning(
                        "better_thermostat %s: Humidity sensor '%s' unavailable at startup. "
                        "Continuing without humidity data.",
                        self.device_name,
                        self.humidity_sensor_entity_id,
                    )
                    self.unavailable_sensors.append(self.humidity_sensor_entity_id)

            if self.outdoor_sensor is not None:
                _out_state = self.hass.states.get(self.outdoor_sensor)
                if _out_state is None or _out_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.warning(
                        "better_thermostat %s: Outdoor sensor '%s' unavailable at startup. "
                        "Will use weather entity as fallback if configured.",
                        self.device_name,
                        self.outdoor_sensor,
                    )
                    self.unavailable_sensors.append(self.outdoor_sensor)

            if self.weather_entity is not None:
                _weather_state = self.hass.states.get(self.weather_entity)
                if _weather_state is None or _weather_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.warning(
                        "better_thermostat %s: Weather entity '%s' unavailable at startup. "
                        "Continuing with call_for_heat=True as default.",
                        self.device_name,
                        self.weather_entity,
                    )
                    self.unavailable_sensors.append(self.weather_entity)

            # Set degraded_mode flag if any sensors are unavailable
            if self.unavailable_sensors:
                self.degraded_mode = True
                _LOGGER.warning(
                    "better_thermostat %s: Starting in DEGRADED MODE. Unavailable sensors: %s",
                    self.device_name,
                    ", ".join(self.unavailable_sensors),
                )

            states = [
                state
                for entity_id in self.real_trvs
                if (state := self.hass.states.get(entity_id)) is not None
            ]

            # Include cooler entity in min/max calculation to ensure BT's
            # temperature range is compatible with all controlled devices
            if self.cooler_entity_id is not None:
                cooler_state = self.hass.states.get(self.cooler_entity_id)
                if cooler_state is not None and cooler_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    states.append(cooler_state)

            self.bt_min_temp = reduce_attribute(states, ATTR_MIN_TEMP, reduce=max)
            self.bt_max_temp = reduce_attribute(states, ATTR_MAX_TEMP, reduce=min)

            if (
                self.bt_min_temp is not None
                and self.bt_max_temp is not None
                and self.bt_min_temp > self.bt_max_temp
            ):
                _LOGGER.warning(
                    "better_thermostat %s: min temp (%.1f°) > max temp (%.1f°). "
                    "This indicates non-overlapping temperature ranges between "
                    "heater and cooler entities. Please check your configuration.",
                    self.device_name,
                    self.bt_min_temp,
                    self.bt_max_temp,
                )

            if self.bt_target_temp_step == 0.0:
                self.bt_target_temp_step = reduce_attribute(
                    states, ATTR_TARGET_TEMP_STEP, reduce=max
                )

            self.all_entities.append(self.sensor_entity_id)

            # Handle room temperature sensor with TRV fallback
            if sensor_state is not None and sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                self.cur_temp = convert_to_float(
                    str(sensor_state.state), self.device_name, "startup()"
                )
            else:
                # Fallback to TRV internal temperature
                _LOGGER.warning(
                    "better_thermostat %s: Room temperature sensor '%s' unavailable. "
                    "Falling back to TRV internal temperature.",
                    self.device_name,
                    self.sensor_entity_id,
                )
                if self.sensor_entity_id not in self.unavailable_sensors:
                    self.unavailable_sensors.append(self.sensor_entity_id)
                    self.degraded_mode = True
                # Get temperature from first available TRV
                self.cur_temp = None
                for trv_id in self.real_trvs.keys():
                    trv_state = self.hass.states.get(trv_id)
                    if trv_state is not None:
                        trv_temp = trv_state.attributes.get("current_temperature")
                        if trv_temp is not None:
                            self.cur_temp = convert_to_float(
                                str(trv_temp),
                                self.device_name,
                                "startup() TRV fallback",
                            )
                            _LOGGER.info(
                                "better_thermostat %s: Using TRV '%s' temperature: %.1f°C",
                                self.device_name,
                                trv_id,
                                self.cur_temp if self.cur_temp else 0,
                            )
                            break
                if self.cur_temp is None:
                    self.cur_temp = DEFAULT_FALLBACK_TEMPERATURE
                    _LOGGER.warning(
                        "better_thermostat %s: No temperature available, using default %.1f°C",
                        self.device_name,
                        DEFAULT_FALLBACK_TEMPERATURE,
                    )

            # Initialize EMA with current temperature at startup
            if self.cur_temp is not None:
                self.last_known_external_temp = self.cur_temp
                try:
                    from .events.temperature import _update_external_temp_ema

                    _update_external_temp_ema(self, float(self.cur_temp))
                    _LOGGER.debug(
                        "better_thermostat %s: initialized external_temp_ema at startup with %.2f",
                        self.device_name,
                        self.cur_temp,
                    )
                except (ValueError, TypeError, ImportError) as e:
                    _LOGGER.warning(
                        "better_thermostat %s: failed to initialize external_temp_ema at startup: %s",
                        self.device_name,
                        e,
                    )

            if self.humidity_sensor_entity_id is not None:
                self.all_entities.append(self.humidity_sensor_entity_id)
                _hum_state = self.hass.states.get(self.humidity_sensor_entity_id)
                if _hum_state is not None and _hum_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    self._current_humidity = convert_to_float(
                        str(_hum_state.state), self.device_name, "startup()"
                    )
                # else: already logged warning above, _current_humidity stays None

            if self.cooler_entity_id is not None:
                _cooler_state = self.hass.states.get(self.cooler_entity_id)
                if _cooler_state is not None and _cooler_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    self.bt_target_cooltemp = convert_to_float(
                        str(_cooler_state.attributes.get("temperature")),
                        self.device_name,
                        "startup()",
                    )
                # else: already logged warning above

            if self.window_id is not None:
                self.all_entities.append(self.window_id)
                window = self.hass.states.get(self.window_id)

                if window is not None and window.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    check = window.state
                    if check in ("on", "open", "true"):
                        self.window_open = True
                    else:
                        self.window_open = False
                    _LOGGER.debug(
                        "better_thermostat %s: detected window state at startup: %s",
                        self.device_name,
                        "Open" if self.window_open else "Closed",
                    )
                else:
                    # Window sensor unavailable - assume closed (safer default)
                    self.window_open = False
                    _LOGGER.debug(
                        "better_thermostat %s: window sensor unavailable, assuming closed",
                        self.device_name,
                    )
            else:
                self.window_open = False

            # Check If we have an old state
            _LOGGER.debug(
                "better_thermostat %s: calling async_get_last_state", self.device_name
            )
            old_state = await self.async_get_last_state()
            _LOGGER.debug(
                "better_thermostat %s: async_get_last_state returned", self.device_name
            )
            if old_state is not None:
                _LOGGER.debug(
                    "better_thermostat %s: restoring state...", self.device_name
                )
                # Restore external_temp_ema if available (overwrites startup init)
                if "external_temp_ema" in old_state.attributes:
                    try:
                        _restored_ema = float(old_state.attributes["external_temp_ema"])
                        self.external_temp_ema = _restored_ema
                        self.cur_temp_filtered = round(_restored_ema, 2)
                        # Reset timestamp to now so the next delta is calculated from restart time
                        self._external_temp_ema_ts = monotonic()
                        _LOGGER.debug(
                            "better_thermostat %s: restored external_temp_ema from state: %.2f",
                            self.device_name,
                            _restored_ema,
                        )
                    except (ValueError, TypeError):
                        pass

                # Restore temp_slope if available
                if "temp_slope_K_min" in old_state.attributes:
                    try:
                        _restored_slope = float(
                            old_state.attributes["temp_slope_K_min"]
                        )
                        self.temp_slope = _restored_slope
                        _LOGGER.debug(
                            "better_thermostat %s: restored temp_slope from state: %.4f",
                            self.device_name,
                            _restored_slope,
                        )
                    except (ValueError, TypeError):
                        pass

                _LOGGER.debug(
                    "better_thermostat %s: restoring target temperature...",
                    self.device_name,
                )
                # If we have no initial temperature, restore
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self.bt_target_temp = reduce_attribute(
                        states, ATTR_TEMPERATURE, reduce=lambda *data: mean(data)
                    )
                    _LOGGER.debug(
                        "better_thermostat %s: Undefined target temperature, falling back to %s",
                        self.device_name,
                        self.bt_target_temp,
                    )
                else:
                    _oldtarget_temperature = float(
                        old_state.attributes.get(ATTR_TEMPERATURE)
                    )
                    # if the saved temperature is lower than the min_temp, set it to min_temp
                    if _oldtarget_temperature < self.bt_min_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is lower than min_temp %s, setting to min_temp",
                            self.device_name,
                            _oldtarget_temperature,
                            self.bt_min_temp,
                        )
                        _oldtarget_temperature = self.bt_min_temp
                    # if the saved temperature is higher than the max_temp, set it to max_temp
                    elif _oldtarget_temperature > self.bt_max_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is higher than max_temp %s, setting to max_temp",
                            self.device_name,
                            _oldtarget_temperature,
                            self.bt_min_temp,
                        )
                        _oldtarget_temperature = self.bt_max_temp
                    self.bt_target_temp = convert_to_float(
                        str(_oldtarget_temperature), self.device_name, "startup()"
                    )
                _LOGGER.debug(
                    "better_thermostat %s: target temperature restored",
                    self.device_name,
                )

                _LOGGER.debug(
                    "better_thermostat %s: restoring preset mode...", self.device_name
                )
                # Restore preset mode if present
                _old_preset = old_state.attributes.get("preset_mode")
                if _old_preset in (
                    [PRESET_NONE] + list(self._preset_temperatures.keys())
                ):
                    self._preset_mode = _old_preset
                else:
                    self._preset_mode = PRESET_NONE

                _LOGGER.debug(
                    "better_thermostat %s: applying restored preset temperature...",
                    self.device_name,
                )
                # If we restored a preset (not NONE) and we have a stored temperature for it,
                # ensure target temp matches (unless the restored target was already equal).
                if (
                    self._preset_mode is not None
                    and self._preset_mode != PRESET_NONE
                    and self._preset_mode in self._preset_temperatures
                ):
                    preset_temp = self._preset_temperatures[self._preset_mode]
                    # Only override if different to avoid masking manual restore logic
                    if (
                        isinstance(preset_temp, (int, float))
                        and preset_temp is not None
                        and self.bt_target_temp != preset_temp
                    ):
                        _LOGGER.debug(
                            "better_thermostat %s: Applying restored preset %s temperature %s after startup",
                            self.device_name,
                            self._preset_mode,
                            preset_temp,
                        )
                        self.bt_target_temp = preset_temp
                _LOGGER.debug(
                    "better_thermostat %s: restored preset temperature applied",
                    self.device_name,
                )

                _LOGGER.debug(
                    "better_thermostat %s: restoring other attributes...",
                    self.device_name,
                )
                if old_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                    self.bt_hvac_mode = old_state.state
                if old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT, None) is not None:
                    self.call_for_heat = old_state.attributes.get(
                        ATTR_STATE_CALL_FOR_HEAT
                    )
                if (
                    old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE, None)
                    is not None
                ):
                    self._saved_temperature = convert_to_float(
                        str(
                            old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE, None)
                        ),
                        self.device_name,
                        "startup()",
                    )
                if old_state.attributes.get(ATTR_STATE_HUMIDIY, None) is not None:
                    self._current_humidity = old_state.attributes.get(
                        ATTR_STATE_HUMIDIY
                    )
                if old_state.attributes.get(ATTR_STATE_MAIN_MODE, None) is not None:
                    self.last_main_hvac_mode = old_state.attributes.get(
                        ATTR_STATE_MAIN_MODE
                    )
                if old_state.attributes.get(ATTR_STATE_HEATING_POWER, None) is not None:
                    loaded_power = float(
                        old_state.attributes.get(ATTR_STATE_HEATING_POWER)
                    )
                    # Bound to realistic values to prevent issues from incorrectly learned values
                    bounded_power = max(
                        MIN_HEATING_POWER, min(MAX_HEATING_POWER, loaded_power)
                    )
                    if bounded_power != loaded_power:
                        _LOGGER.info(
                            "better_thermostat %s: Restored heating_power %.3f "
                            "is outside allowed range [%s, %s]; clamped to %.3f",
                            self.device_name,
                            loaded_power,
                            MIN_HEATING_POWER,
                            MAX_HEATING_POWER,
                            bounded_power,
                        )
                    self.heating_power = bounded_power
                elif getattr(self, "_thermal_store", None) is not None:
                    # Fallback: restore heating_power from persistent thermal stats
                    try:
                        data = await self._thermal_store.async_load()
                        key = str(self._config_entry_id)
                        if data and key in data and "heating_power" in data[key]:
                            loaded_power = float(data[key]["heating_power"])
                            bounded_power = max(
                                MIN_HEATING_POWER, min(MAX_HEATING_POWER, loaded_power)
                            )
                            self.heating_power = bounded_power
                    except Exception:
                        pass

                # Restore heat loss if available
                if old_state.attributes.get(ATTR_STATE_HEAT_LOSS, None) is not None:
                    try:
                        loaded_loss = float(
                            old_state.attributes.get(ATTR_STATE_HEAT_LOSS)
                        )
                        bounded_loss = max(
                            MIN_HEAT_LOSS, min(MAX_HEAT_LOSS, loaded_loss)
                        )
                        self.heat_loss_rate = bounded_loss
                    except (TypeError, ValueError):
                        pass
                elif getattr(self, "_thermal_store", None) is not None:
                    try:
                        data = await self._thermal_store.async_load()
                        key = str(self._config_entry_id)
                        if data and key in data and "heat_loss" in data[key]:
                            loaded_loss = float(data[key]["heat_loss"])
                            bounded_loss = max(
                                MIN_HEAT_LOSS, min(MAX_HEAT_LOSS, loaded_loss)
                            )
                            self.heat_loss_rate = bounded_loss
                    except (ValueError, TypeError, KeyError):
                        pass
                if (
                    old_state.attributes.get(ATTR_STATE_PRESET_TEMPERATURE, None)
                    is not None
                ):
                    self._preset_temperature = convert_to_float(
                        str(
                            old_state.attributes.get(
                                ATTR_STATE_PRESET_TEMPERATURE, None
                            )
                        ),
                        self.device_name,
                        "startup()",
                    )
                # Restore preset mode
                if old_state.attributes.get("preset_mode", None) is not None:
                    restored_preset = old_state.attributes.get("preset_mode")
                    if restored_preset in self.preset_modes:
                        self._preset_mode = restored_preset
                        _LOGGER.debug(
                            "better_thermostat %s: Restored preset mode: %s",
                            self.device_name,
                            restored_preset,
                        )
                _LOGGER.debug(
                    "better_thermostat %s: state restoration completed",
                    self.device_name,
                )

                # ECO mode state / saved ECO temperature not restored; Eco preset is supported via PRESET_ECO.

            else:
                # No previous state, try and restore defaults
                _LOGGER.debug(
                    "better_thermostat %s: no previous state, restoring defaults...",
                    self.device_name,
                )
                if self.bt_target_temp is None or not isinstance(
                    self.bt_target_temp, float
                ):
                    _LOGGER.info(
                        "better_thermostat %s: No previously saved temperature found on startup, get it from the TRV",
                        self.device_name,
                    )
                    self.bt_target_temp = reduce_attribute(
                        states, ATTR_TEMPERATURE, reduce=lambda *data: mean(data)
                    )
                _LOGGER.debug(
                    "better_thermostat %s: defaults restored", self.device_name
                )

            # if hvac mode could not be restored, turn heat off
            _LOGGER.debug(
                "better_thermostat %s: checking hvac mode...", self.device_name
            )
            if self.bt_hvac_mode in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                current_hvac_modes = [
                    x.state for x in states if x.state != HVACMode.OFF
                ]
                # return the most common hvac mode (what the thermostat is set to do) except OFF
                if current_hvac_modes:
                    _temp_bt_hvac_mode = max(
                        set(current_hvac_modes), key=current_hvac_modes.count
                    )
                    if _temp_bt_hvac_mode is not HVACMode.OFF:
                        self.bt_hvac_mode = HVACMode.HEAT
                    else:
                        self.bt_hvac_mode = HVACMode.OFF
                    _LOGGER.debug(
                        "better_thermostat %s: No previously hvac mode found on startup, turn bt to trv mode %s",
                        self.device_name,
                        self.bt_hvac_mode,
                    )
                # return off if all are off
                elif all(x.state == HVACMode.OFF for x in states):
                    self.bt_hvac_mode = HVACMode.OFF
                    _LOGGER.debug(
                        "better_thermostat %s: No previously hvac mode found on startup, turn bt to trv mode %s",
                        self.device_name,
                        self.bt_hvac_mode,
                    )
                else:
                    _LOGGER.warning(
                        "better_thermostat %s: No previously hvac mode found on startup, turn heat off",
                        self.device_name,
                    )
                    self.bt_hvac_mode = HVACMode.OFF

            _LOGGER.debug(
                "better_thermostat %s: Startup config, BT hvac mode is %s, Target temp %s",
                self.device_name,
                self.bt_hvac_mode,
                self.bt_target_temp,
            )

            if self.last_main_hvac_mode is None:
                self.last_main_hvac_mode = self.bt_hvac_mode

            _LOGGER.debug(
                "better_thermostat %s: checking humidity sensor...", self.device_name
            )
            if self.humidity_sensor_entity_id is not None:
                _hum_state = self.hass.states.get(self.humidity_sensor_entity_id)
                if _hum_state is None:
                    _LOGGER.warning(
                        "better_thermostat %s: Humidity sensor %s not found or not ready",
                        self.device_name,
                        self.humidity_sensor_entity_id,
                    )
                    self._current_humidity = 0
                else:
                    self._current_humidity = convert_to_float(
                        str(_hum_state.state), self.device_name, "startup()"
                    )
            else:
                self._current_humidity = 0

            self.last_window_state = self.window_open
            if self.bt_hvac_mode not in (
                HVACMode.OFF,
                HVACMode.HEAT_COOL,
                HVACMode.HEAT,
            ):
                self.bt_hvac_mode = HVACMode.HEAT

            _LOGGER.debug(
                "better_thermostat %s: writing initial state...", self.device_name
            )
            self.async_write_ha_state()

            for trv in self.real_trvs.keys():
                self.all_entities.append(trv)
                _LOGGER.debug(
                    "better_thermostat %s: initializing TRV %s", self.device_name, trv
                )
                try:
                    await asyncio.wait_for(init(self, trv), timeout=30)
                    _LOGGER.debug(
                        "better_thermostat %s: TRV %s initialized",
                        self.device_name,
                        trv,
                    )
                except TimeoutError:
                    _LOGGER.error(
                        "better_thermostat %s: Timeout initializing TRV %s",
                        self.device_name,
                        trv,
                    )
                except Exception as exc:
                    _LOGGER.error(
                        "better_thermostat %s: Error initializing TRV %s: %s",
                        self.device_name,
                        trv,
                        exc,
                    )

                try:
                    await inital_tweak(self, trv)
                except Exception as exc:
                    _LOGGER.error(
                        "better_thermostat %s: Error running initial tweak for TRV %s: %s",
                        self.device_name,
                        trv,
                        exc,
                    )

                if self.real_trvs[trv]["calibration"] != 1:
                    _LOGGER.debug(
                        "better_thermostat %s: getting offsets for TRV %s",
                        self.device_name,
                        trv,
                    )

                    try:
                        async with asyncio.timeout(10):
                            self.real_trvs[trv][
                                "last_calibration"
                            ] = await get_current_offset(self, trv)
                            self.real_trvs[trv][
                                "local_calibration_min"
                            ] = await get_min_offset(self, trv)
                            self.real_trvs[trv][
                                "local_calibration_max"
                            ] = await get_max_offset(self, trv)
                            self.real_trvs[trv][
                                "local_calibration_step"
                            ] = await get_offset_step(self, trv)
                        # Ensure None values are replaced with sensible defaults
                        self._set_trv_calibration_defaults(trv)
                        _LOGGER.debug(
                            "better_thermostat %s: offsets for TRV %s retrieved",
                            self.device_name,
                            trv,
                        )
                    except TimeoutError:
                        _LOGGER.error(
                            "better_thermostat %s: Timeout getting offsets for TRV %s",
                            self.device_name,
                            trv,
                        )
                        self._set_trv_calibration_defaults(trv)
                    except Exception as exc:
                        _LOGGER.error(
                            "better_thermostat %s: Error getting offsets for TRV %s: %s",
                            self.device_name,
                            trv,
                            exc,
                        )
                        self._set_trv_calibration_defaults(trv)
                else:
                    self.real_trvs[trv]["last_calibration"] = 0
                    self.real_trvs[trv]["local_calibration_min"] = -7
                    self.real_trvs[trv]["local_calibration_max"] = 7
                    self.real_trvs[trv]["local_calibration_step"] = 0.5

                _s = self.hass.states.get(trv)
                _attrs = _s.attributes if _s else {}
                _LOGGER.debug(
                    "better_thermostat %s: reading TRV %s attributes...",
                    self.device_name,
                    trv,
                )
                self.real_trvs[trv]["valve_position"] = convert_to_float(
                    str(_attrs.get("valve_position", None)), self.device_name, "startup"
                )
                self.real_trvs[trv]["max_temp"] = convert_to_float(
                    str(_attrs.get("max_temp", 30)), self.device_name, "startup"
                )
                self.real_trvs[trv]["min_temp"] = convert_to_float(
                    str(_attrs.get("min_temp", 5)), self.device_name, "startup"
                )
                # Prefer configured step over device-reported step
                cfg_step = (
                    self.bt_target_temp_step
                    if self.bt_target_temp_step and self.bt_target_temp_step > 0.0
                    else None
                )
                if cfg_step is not None:
                    self.real_trvs[trv]["target_temp_step"] = cfg_step
                else:
                    self.real_trvs[trv]["target_temp_step"] = convert_to_float(
                        str(_attrs.get("target_temp_step", 0.5)),
                        self.device_name,
                        "startup",
                    )
                self.real_trvs[trv]["temperature"] = convert_to_float(
                    str(_attrs.get("temperature", 5)), self.device_name, "startup"
                )
                self.real_trvs[trv]["hvac_modes"] = _attrs.get("hvac_modes", None)
                self.real_trvs[trv]["hvac_mode"] = _s.state if _s else None
                self.real_trvs[trv]["last_hvac_mode"] = _s.state if _s else None
                self.real_trvs[trv]["last_temperature"] = convert_to_float(
                    str(_attrs.get("temperature")), self.device_name, "startup()"
                )
                self.real_trvs[trv]["current_temperature"] = convert_to_float(
                    str(_attrs.get("current_temperature") or 5),
                    self.device_name,
                    "startup()",
                )
                _LOGGER.debug(
                    "better_thermostat %s: controlling TRV %s...", self.device_name, trv
                )
                try:
                    await asyncio.wait_for(control_trv(self, trv), timeout=10)
                    _LOGGER.debug(
                        "better_thermostat %s: TRV %s controlled", self.device_name, trv
                    )
                except TimeoutError:
                    _LOGGER.error(
                        "better_thermostat %s: Timeout controlling TRV %s",
                        self.device_name,
                        trv,
                    )
                except Exception as exc:
                    _LOGGER.error(
                        "better_thermostat %s: Error controlling TRV %s: %s",
                        self.device_name,
                        trv,
                        exc,
                    )

            _LOGGER.debug("better_thermostat %s: triggering time...", self.device_name)
            await self._trigger_time(None)
            _LOGGER.debug(
                "better_thermostat %s: triggering check weather...", self.device_name
            )
            await self._trigger_check_weather(None)
            _LOGGER.debug(
                "better_thermostat %s: startup finishing...", self.device_name
            )
            self.startup_running = False
            self._available = True
            self.async_write_ha_state()

            _LOGGER.debug("better_thermostat %s: sleeping 15s...", self.device_name)
            await asyncio.sleep(15)
            _LOGGER.debug(
                "better_thermostat %s: finding battery entities...", self.device_name
            )

            # try to find battery entities for all related entities
            for entity in self.all_entities:
                if entity is not None:
                    battery_id = await find_battery_entity(self, entity)
                    if battery_id is not None:
                        self.devices_states[entity] = {
                            "battery_id": battery_id,
                            "battery": None,
                        }

            if self.is_removed:
                return

            # Add listener
            if self.outdoor_sensor is not None:
                self.all_entities.append(self.outdoor_sensor)
                self.async_on_remove(
                    async_track_time_change(self.hass, self._trigger_time, 5, 0, 0)
                )

            _LOGGER.debug(
                "better_thermostat %s: checking critical entities...", self.device_name
            )
            await check_critical_entities(self)
            await check_and_update_degraded_mode(self)

            if self.is_removed:
                return

            _LOGGER.debug(
                "better_thermostat %s: registering periodic tasks...", self.device_name
            )
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._trigger_check_weather, timedelta(hours=1)
                )
            )

            # Periodischer 5-Minuten-Tick: nur aktivieren, wenn Balance konfiguriert ist
            balance_modes = {"heuristic", "pid"}
            active_balance_modes = set()
            active_calibration_modes = set()
            try:
                for trv_info in self.real_trvs.values():
                    advanced = trv_info.get("advanced", {}) or {}

                    raw_balance = advanced.get("balance_mode", "")
                    balance_value = getattr(raw_balance, "value", raw_balance)
                    if isinstance(balance_value, str):
                        balance_mode = balance_value.lower()
                        if balance_mode in balance_modes:
                            active_balance_modes.add(balance_mode)

                    raw_calibration = advanced.get("calibration_mode", "")
                    calibration_value = getattr(
                        raw_calibration, "value", raw_calibration
                    )
                    if isinstance(calibration_value, str):
                        calibration_mode = calibration_value.lower()
                        if calibration_mode in (
                            CalibrationMode.DEFAULT.value,
                            CalibrationMode.MPC_CALIBRATION.value,
                            CalibrationMode.TPI_CALIBRATION.value,
                            CalibrationMode.PID_CALIBRATION.value,
                        ):
                            active_calibration_modes.add(calibration_mode)
            except Exception:
                active_balance_modes = set()
                active_calibration_modes = set()

            if active_balance_modes or active_calibration_modes:
                self.async_on_remove(
                    async_track_time_interval(
                        self.hass, self._trigger_time, timedelta(minutes=5)
                    )
                )
                _LOGGER.debug(
                    "better_thermostat %s: 5min periodic tick enabled (balance_modes=%s calibration_modes=%s)",
                    self.device_name,
                    sorted(active_balance_modes),
                    sorted(active_calibration_modes),
                )
            else:
                _LOGGER.debug(
                    "better_thermostat %s: 5min periodic tick skipped (no supported balance/calibration mode)",
                    self.device_name,
                )

            # Periodischer Keepalive: externe Temperatur mindestens alle 30 Minuten an TRVs senden
            self.async_on_remove(
                async_track_time_interval(
                    self.hass,
                    self._external_temperature_keepalive,
                    timedelta(minutes=30),
                )
            )

            # Ventilwartung: separaten Tick nur aktivieren, wenn mindestens ein TRV sie eingeschaltet hat
            try:
                any_maintenance = any(
                    bool(
                        (trv_info.get("advanced", {}) or {}).get(
                            CONF_VALVE_MAINTENANCE, False
                        )
                    )
                    for trv_info in self.real_trvs.values()
                )
            except Exception:
                any_maintenance = False

            if any_maintenance:
                # Re-calculate next maintenance based on loaded TRV quirks
                # (overrides the random 1h-5d startup default)
                min_interval_hours = 168  # Default 7 days
                for trv_id in self.real_trvs.keys():
                    if bool(
                        (self.real_trvs[trv_id].get("advanced", {}) or {}).get(
                            CONF_VALVE_MAINTENANCE, False
                        )
                    ):
                        quirks = (self.real_trvs.get(trv_id, {}) or {}).get(
                            "model_quirks"
                        )
                        interval = int(
                            getattr(quirks, "VALVE_MAINTENANCE_INTERVAL_HOURS", 168)
                        )
                        min_interval_hours = min(min_interval_hours, interval)

                now = datetime.now()
                # Schedule initial run: randomize within [1h, min(5d, interval)]
                # If interval is very short (e.g. 12h), respect it.
                max_delay_hours = min(24 * 5, min_interval_hours)
                delay_hours = randint(1, max(2, max_delay_hours))

                self.next_valve_maintenance = now + timedelta(hours=delay_hours)

                self.async_on_remove(
                    async_track_time_interval(
                        self.hass, self._maintenance_tick, timedelta(minutes=5)
                    )
                )
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance tick enabled (5min), first run at %s",
                    self.device_name,
                    self.next_valve_maintenance,
                )
            else:
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance tick skipped (no TRV enabled)",
                    self.device_name,
                )

            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.sensor_entity_id], self._trigger_temperature_change
                )
            )
            if self.humidity_sensor_entity_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass,
                        [self.humidity_sensor_entity_id],
                        self._trigger_humidity_change,
                    )
                )
            if self._async_unsub_state_changed is None:
                self._async_unsub_state_changed = async_track_state_change_event(
                    self.hass, self.entity_ids, self._trigger_trv_change
                )
                self.async_on_remove(self._async_unsub_state_changed)
            if self.window_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass, [self.window_id], self._trigger_window_change
                    )
                )
            if self.cooler_entity_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass, [self.cooler_entity_id], self._tigger_cooler_change
                    )
                )
            # Sende initial sofort einen Keepalive, damit TRVs nicht bis zum ersten 30min-Tick warten müssen
            try:
                _LOGGER.debug(
                    "better_thermostat %s: creating keepalive task...", self.device_name
                )
                self.hass.async_create_task(self._external_temperature_keepalive())
            except Exception as exc:
                _LOGGER.error(
                    "better_thermostat %s: Failed to create external temperature keepalive task: %s",
                    self.device_name,
                    exc,
                )
            # Start periodic EMA update (every minute)
            _LOGGER.debug(
                "better_thermostat %s: starting EMA timer...", self.device_name
            )
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._async_update_ema_periodic, timedelta(minutes=1)
                )
            )
            _LOGGER.info("better_thermostat %s: startup completed.", self.device_name)
            self.async_write_ha_state()
            await self.async_update_ha_state(force_refresh=True)
            break

    async def _maintenance_tick(self, event=None):
        """Periodic maintenance tick: runs valve exercise when due and enabled."""
        # quick availability check - only critical entities needed for maintenance
        try:
            ok = await check_critical_entities(self)
            if ok is False:
                return
            await check_and_update_degraded_mode(self)
        except Exception:
            return

        # Skip if already running or not due
        now = datetime.now()
        if self.in_maintenance:
            return
        try:
            if self.next_valve_maintenance and now < self.next_valve_maintenance:
                return
        except Exception:
            pass

        # Skip when device is OFF or window open
        if self.window_open:
            # postpone by 6 hours to avoid hammering
            self.next_valve_maintenance = now + timedelta(hours=1)
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance postponed (window open)",
                self.device_name,
            )
            return
        if HVACMode.OFF in (self.hvac_mode, self.bt_hvac_mode):
            self.next_valve_maintenance = now + timedelta(hours=1)
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance postponed (HVAC OFF)",
                self.device_name,
            )
            return

        # Check if any TRV actually has maintenance enabled
        trvs_to_service: list[str] = []
        try:
            for trv_id, info in self.real_trvs.items():
                adv = info.get("advanced", {}) or {}
                if bool(adv.get(CONF_VALVE_MAINTENANCE, False)):
                    trvs_to_service.append(trv_id)
        except Exception:
            trvs_to_service = []

        if not trvs_to_service:
            # no enabled TRVs => schedule far in the future to avoid frequent wakeups
            self.next_valve_maintenance = now + timedelta(days=7)
            return

        # Run maintenance asynchronously (don't block the tick)
        self.hass.async_create_task(self._run_valve_maintenance(trvs_to_service))

    async def _run_valve_maintenance(self, trvs: list[str]) -> None:
        """Perform valve exercise: open fully, then close, restore state, and reschedule."""
        if self.in_maintenance:
            return
        self.in_maintenance = True
        # Suppress control loop briefly
        self.ignore_states = True
        now = datetime.now()

        try:
            _LOGGER.info(
                "better_thermostat %s: starting valve maintenance for %d TRV(s)",
                self.device_name,
                len(trvs),
            )

            # Snapshot TRV states & determine method per TRV
            trv_infos: dict[str, dict] = {}

            # Helper to set valve percent; delegate records last percent/method.
            async def _set_valve_pct(trv_id: str, pct: int) -> bool:
                try:
                    from .adapters.delegate import set_valve as _delegate_set_valve

                    ok = await _delegate_set_valve(self, trv_id, int(pct))
                    return bool(ok)
                except Exception:
                    return False

            for trv_id in trvs:
                # Per-TRV guard
                try:
                    self.real_trvs[trv_id]["ignore_trv_states"] = True
                except Exception:
                    pass

                trv_state = self.hass.states.get(trv_id)
                if trv_state is None:
                    _LOGGER.debug(
                        "better_thermostat %s: maintenance skip %s (state None)",
                        self.device_name,
                        trv_id,
                    )
                    # Release guard for this TRV (we won't touch it)
                    try:
                        self.real_trvs[trv_id]["ignore_trv_states"] = False
                    except Exception:
                        pass
                    continue

                cur_mode = trv_state.state
                cur_temp = trv_state.attributes.get("temperature")

                valve_entity = (self.real_trvs.get(trv_id, {}) or {}).get(
                    "valve_position_entity"
                )
                quirks = (self.real_trvs.get(trv_id, {}) or {}).get("model_quirks")
                support_valve = bool(valve_entity) or bool(
                    getattr(quirks, "override_set_valve", None)
                )
                _calibration_type = (
                    (self.real_trvs.get(trv_id, {}) or {})
                    .get("advanced", {})
                    .get("calibration")
                )

                use_direct_valve = bool(
                    support_valve
                    and _calibration_type == CalibrationType.DIRECT_VALVE_BASED
                )

                trv_infos[trv_id] = {
                    "cur_mode": cur_mode,
                    "cur_temp": cur_temp,
                    "use_direct_valve": use_direct_valve,
                    "max_t": (self.real_trvs.get(trv_id, {}) or {}).get("max_temp", 30),
                    "min_t": (self.real_trvs.get(trv_id, {}) or {}).get("min_temp", 5),
                }

            async def _open_step(trv_id: str):
                info = trv_infos.get(trv_id)
                if not info:
                    return
                if info["use_direct_valve"]:
                    await _set_valve_pct(trv_id, 100)
                    return
                # temp-extremes fallback: only when TRV is not OFF
                if info["cur_mode"] != HVACMode.OFF:
                    await adapter_set_temperature(self, trv_id, info["max_t"])

            async def _close_step(trv_id: str):
                info = trv_infos.get(trv_id)
                if not info:
                    return
                if info["use_direct_valve"]:
                    await _set_valve_pct(trv_id, 0)
                    return
                if info["cur_mode"] != HVACMode.OFF:
                    await adapter_set_temperature(self, trv_id, info["min_t"])

            # Execute in synchronized steps across all TRVs (much faster than sequential).
            # Open all -> wait -> close all -> wait (repeat twice)
            for i in range(2):
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance cycle %d/2 starting for %d TRV(s)",
                    self.device_name,
                    i + 1,
                    len(trv_infos),
                )
                await asyncio.gather(
                    *(_open_step(trv_id) for trv_id in trv_infos.keys()),
                    return_exceptions=True,
                )
                await asyncio.sleep(30)
                await asyncio.gather(
                    *(_close_step(trv_id) for trv_id in trv_infos.keys()),
                    return_exceptions=True,
                )
                await asyncio.sleep(30)

            # Restore previous setpoint and mode for all TRVs
            async def _restore_one(trv_id: str):
                info = trv_infos.get(trv_id)
                if not info:
                    return
                try:
                    if info.get("cur_temp") is not None:
                        await adapter_set_temperature(self, trv_id, info["cur_temp"])
                except Exception:
                    pass
                try:
                    await adapter_set_hvac_mode(self, trv_id, info["cur_mode"])
                except Exception:
                    pass
                try:
                    self.real_trvs[trv_id]["ignore_trv_states"] = False
                except Exception:
                    pass

            await asyncio.gather(
                *(_restore_one(trv_id) for trv_id in trv_infos.keys()),
                return_exceptions=True,
            )

            # Ensure we always release the guard for TRVs that were skipped above.
            for trv_id in trvs:
                if trv_id in trv_infos:
                    continue
                try:
                    self.real_trvs[trv_id]["ignore_trv_states"] = False
                except Exception:
                    pass

            # Determine next maintenance interval based on the quirks of enabled TRVs
            min_interval_hours = 168  # Default 7 days
            for trv_id in trvs:
                quirks = (self.real_trvs.get(trv_id, {}) or {}).get("model_quirks")
                # Default to 168 hours if quirk doesn't specify
                interval = int(getattr(quirks, "VALVE_MAINTENANCE_INTERVAL_HOURS", 168))
                min_interval_hours = min(min_interval_hours, interval)

            # Add ~7% randomization
            variance = max(1, int(min_interval_hours * 0.07))
            self.next_valve_maintenance = now + timedelta(
                hours=min_interval_hours + randint(0, variance)
            )
            _LOGGER.info(
                "better_thermostat %s: valve maintenance finished; next at %s",
                self.device_name,
                self.next_valve_maintenance,
            )
        finally:
            self._control_needed_after_maintenance = False
            # Always release ignore_states after maintenance.
            # If we restore a previous True here, the control_queue loop can get stuck
            # sleeping forever and never consume queued control actions.
            self.ignore_states = False
            self.in_maintenance = False

            # Trigger one control cycle after maintenance so BT immediately resumes
            # with the latest window/temp/target states.
            if self.bt_hvac_mode != HVACMode.OFF:
                try:
                    self.control_queue_task.put_nowait(self)
                except Exception:
                    # Queue full or not ready; periodic tick will eventually catch up.
                    pass

    async def _load_pid_state(self) -> None:
        """Load persisted PID states and hydrate module-level cache."""
        if self._pid_store is None:
            return
        data = await self._pid_store.async_load()
        if not data:
            return
        prefix = f"{self._unique_id}:"
        try:
            imported = pid_import_states(data, prefix_filter=prefix)
            _LOGGER.debug(
                "better_thermostat %s: loaded %s PID state(s) with prefix %s",
                self.device_name,
                imported,
                prefix,
            )
        except Exception as e:
            _LOGGER.debug(
                "better_thermostat %s: failed to import balance states: %s",
                self.device_name,
                e,
            )

    async def _save_pid_state(self) -> None:
        """Persist current PID states for this entity (prefix filtered)."""
        if self._pid_store is None:
            return
        try:
            prefix = f"{self._unique_id}:"
            current = pid_export_states(prefix=prefix)
            # Merge with existing store to avoid overwriting other entities' data
            existing = await self._pid_store.async_load()
            if not isinstance(existing, dict):
                existing = {}
            # Drop previous entries for this entity's prefix
            to_delete = [k for k in list(existing.keys()) if str(k).startswith(prefix)]
            for k in to_delete:
                try:
                    del existing[k]
                except KeyError:
                    pass
            # Update with current
            existing.update(current)
            await self._pid_store.async_save(existing)
            _LOGGER.debug(
                "better_thermostat %s: saved %d PID state(s)",
                self.device_name,
                len(current or {}),
            )
        except Exception as e:
            _LOGGER.debug(
                "better_thermostat %s: saving PID states failed: %s",
                self.device_name,
                e,
            )

    def schedule_save_pid_state(self, delay_s: float = 10.0) -> None:
        """Debounced scheduling for saving PID state to storage."""
        if self._pid_store is None or self._pid_save_scheduled:
            return
        self._pid_save_scheduled = True

        async def _delayed_save():
            try:
                await asyncio.sleep(delay_s)
                await self._save_pid_state()
            finally:
                self._pid_save_scheduled = False

        # Fire and forget
        self.hass.async_create_task(_delayed_save())

    async def _load_mpc_states(self) -> None:
        """Load persisted MPC adaptive controller states for this entity."""

        if self._mpc_store is None:
            return
        data = await self._mpc_store.async_load()
        if not isinstance(data, dict):
            return
        prefix = f"{self._unique_id}:"
        scoped: dict[str, dict[str, Any]] = {}
        for key, payload in data.items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            if isinstance(payload, dict):
                scoped[key] = payload
        if scoped:
            import_mpc_state_map(scoped)

    async def _save_mpc_states(self) -> None:
        """Persist MPC adaptive controller states for this entity."""

        if self._mpc_store is None:
            return
        try:
            existing = await self._mpc_store.async_load()
            if not isinstance(existing, dict):
                existing = {}
            prefix = f"{self._unique_id}:"
            for key in list(existing.keys()):
                if isinstance(key, str) and key.startswith(prefix):
                    del existing[key]
            exported = export_mpc_state_map(prefix)
            if exported:
                existing.update(exported)
            await self._mpc_store.async_save(existing)
        except Exception as e:
            _LOGGER.debug(
                "better_thermostat %s: saving MPC states failed: %s",
                self.device_name,
                e,
            )

    def _schedule_save_mpc_states(self, delay_s: float = 15.0) -> None:
        """Debounced scheduling for persisting MPC adaptive states."""

        if self._mpc_store is None or self._mpc_save_scheduled:
            return
        self._mpc_save_scheduled = True

        async def _delayed_save():
            try:
                await asyncio.sleep(delay_s)
                await self._save_mpc_states()
            finally:
                self._mpc_save_scheduled = False

        self.hass.async_create_task(_delayed_save())

    async def _load_tpi_states(self) -> None:
        """Load persisted TPI adaptive controller states for this entity."""

        if self._tpi_store is None:
            return
        data = await self._tpi_store.async_load()
        if not isinstance(data, dict):
            return
        prefix = f"{self._unique_id}:"
        scoped: dict[str, dict[str, Any]] = {}
        for key, payload in data.items():
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            if isinstance(payload, dict):
                scoped[key] = payload
        if scoped:
            import_tpi_state_map(scoped)

    async def _save_tpi_states(self) -> None:
        """Persist TPI adaptive controller states for this entity."""

        if self._tpi_store is None:
            return
        try:
            existing = await self._tpi_store.async_load()
            if not isinstance(existing, dict):
                existing = {}
            prefix = f"{self._unique_id}:"
            for key in list(existing.keys()):
                if isinstance(key, str) and key.startswith(prefix):
                    del existing[key]
            exported = export_tpi_state_map(prefix)
            if exported:
                existing.update(exported)
            await self._tpi_store.async_save(existing)
        except Exception as e:
            _LOGGER.debug(
                "better_thermostat %s: saving TPI states failed: %s",
                self.device_name,
                e,
            )

    def _schedule_save_tpi_states(self, delay_s: float = 15.0) -> None:
        """Debounced scheduling for persisting TPI adaptive states."""

        if self._tpi_store is None or self._tpi_save_scheduled:
            return
        self._tpi_save_scheduled = True

        async def _delayed_save():
            try:
                await asyncio.sleep(delay_s)
                await self._save_tpi_states()
            finally:
                self._tpi_save_scheduled = False

        self.hass.async_create_task(_delayed_save())

    async def _load_thermal_stats(self) -> None:
        """Load persisted thermal stats (heating_power / heat_loss)."""

        if self._thermal_store is None:
            return
        data = await self._thermal_store.async_load()
        if not data:
            return

        key = str(self._config_entry_id)
        payload = data.get(key)
        if not isinstance(payload, dict):
            return

        if "heating_power" in payload:
            try:
                loaded_power = float(payload["heating_power"])
                self.heating_power = max(
                    MIN_HEATING_POWER, min(MAX_HEATING_POWER, loaded_power)
                )
            except (TypeError, ValueError):
                pass

        if "heat_loss" in payload:
            try:
                loaded_loss = float(payload["heat_loss"])
                self.heat_loss_rate = max(
                    MIN_HEAT_LOSS, min(MAX_HEAT_LOSS, loaded_loss)
                )
            except (TypeError, ValueError):
                pass

    async def _save_thermal_stats(self) -> None:
        """Persist thermal stats to storage (debounced)."""

        if self._thermal_store is None:
            return
        key = str(self._config_entry_id)
        payload = {
            "heating_power": getattr(self, "heating_power", None),
            "heat_loss": getattr(self, "heat_loss_rate", None),
        }
        existing = await self._thermal_store.async_load() or {}
        existing[key] = payload
        await self._thermal_store.async_save(existing)

    def _schedule_save_thermal_stats(self, delay_s: float = 15.0) -> None:
        """Debounced scheduling for persisting thermal stats."""

        if self._thermal_store is None or self._thermal_save_scheduled:
            return
        self._thermal_save_scheduled = True

        async def _delayed_save():
            try:
                await asyncio.sleep(delay_s)
                await self._save_thermal_stats()
            finally:
                self._thermal_save_scheduled = False

        self.hass.async_create_task(_delayed_save())

    async def calculate_heating_power(self):
        """Learn effective heating power (°C/min) from completed heating cycles.

        Improvements over the original implementation:
        - Minimum duration of 1 minute (otherwise cycle is ignored)
        - Wait for the post-heating temperature peak (thermal inertia) after HEATING stops
        - Timeout based finalization if the temperature does not fall (prevents stuck cycles)
        - Outdoor temperature (if available) is used for normalization & adaptive weighting
        - Bounded telemetry (deque) for minimal memory footprint
        - Reduced state writes (only on changes / cycle finalization / action switches)
        """

        # Skip if we have no current temperature
        if self.cur_temp is None:
            return

        # Lazy init of target range bounds
        if not hasattr(self, "min_target_temp"):
            self.min_target_temp = self.bt_target_temp or 18.0
        if not hasattr(self, "max_target_temp"):
            self.max_target_temp = self.bt_target_temp or 21.0

        # Telemetry container (create once)
        if not hasattr(self, "heating_cycles"):
            # bounded length (50 cycles)
            self.heating_cycles = deque(maxlen=50)

        now = dt_util.utcnow()  # UTC aware time

        # Determine current action early (pure computation) for transition handling
        current_action = self._compute_hvac_action()

        action_changed = current_action != self.old_attr_hvac_action

        # Transition: heating starts
        if (
            current_action == HVACAction.HEATING
            and self.old_attr_hvac_action != HVACAction.HEATING
        ):
            self.heating_start_temp = self.cur_temp
            self.heating_start_timestamp = now
            self.heating_end_temp = None
            self.heating_end_timestamp = None

        # Transition: heating stops (candidate end)
        elif (
            current_action != HVACAction.HEATING
            and self.old_attr_hvac_action == HVACAction.HEATING
            and self.heating_start_temp is not None
            and self.heating_end_temp is None
        ):
            self.heating_end_temp = self.cur_temp
            self.heating_end_timestamp = now

        # Peak tracking: temperature still rising after heating already stopped
        elif (
            current_action != HVACAction.HEATING
            and self.heating_start_temp is not None
            and self.heating_end_temp is not None
            and self.cur_temp > self.heating_end_temp
        ):
            self.heating_end_temp = self.cur_temp
            self.heating_end_timestamp = now

        # Finalization criteria: temperature drops OR timeout triggers
        finalize = False
        TIMEOUT_MIN = 30  # safety timeout after 30 minutes of plateau

        if (
            self.heating_start_temp is not None
            and self.heating_end_temp is not None
            and self.cur_temp < self.heating_end_temp  # peak passed (temp falling)
        ):
            finalize = True
        elif self.heating_end_timestamp is not None and (
            now - self.heating_end_timestamp
        ) > timedelta(minutes=TIMEOUT_MIN):
            finalize = True

        heating_power_changed = False
        normalized_power = None

        if finalize:
            if (
                self.heating_end_temp is not None
                and self.heating_start_temp is not None
            ):
                temp_diff = self.heating_end_temp - self.heating_start_temp
            else:
                temp_diff = 0
            duration_min = (
                (
                    self.heating_end_timestamp - self.heating_start_timestamp
                ).total_seconds()
                / 60.0
                if self.heating_end_timestamp and self.heating_start_timestamp
                else 0
            )
            # Require minimum duration and positive temperature increase
            if duration_min >= 1.0 and temp_diff > 0:
                # Base weighting via relative position within target range
                temp_range = max(self.max_target_temp - self.min_target_temp, 0.1)
                relative_pos = (
                    (self.bt_target_temp - self.min_target_temp) / temp_range
                    if self.bt_target_temp is not None
                    else 0.5
                )
                weight_factor = max(0.5, min(1.5, 0.5 + relative_pos))

                # Consider outdoor temperature if available
                outdoor = None
                try:
                    if self.outdoor_sensor is not None:
                        outdoor_state = self.hass.states.get(self.outdoor_sensor)
                        if outdoor_state is not None:
                            outdoor = convert_to_float(
                                str(outdoor_state.state),
                                self.device_name,
                                "calculate_heating_power.outdoor",
                            )
                except Exception:
                    outdoor = None

                # Environmental delta (setpoint - outdoor) for normalization
                if outdoor is not None and self.bt_target_temp is not None:
                    delta_env = max(self.bt_target_temp - outdoor, 0.1)
                    # Normalized heating rate (°C/min relative to thermal gradient)
                    normalized_power = round((temp_diff / duration_min) / delta_env, 5)
                    # Environment factor influences smoothing weight (larger gradient -> slightly higher weight)
                    env_factor = max(0.7, min(1.3, delta_env / 20.0))
                else:
                    env_factor = 1.0

                heating_rate = round(temp_diff / duration_min, 4)  # °C / min

                # Adaptive exponential smoothing (alpha)
                base_alpha = 0.10
                alpha = base_alpha * weight_factor * env_factor
                alpha = max(0.02, min(0.25, alpha))  # Bounds

                old_power = self.heating_power
                unbounded_new_power = old_power * (1 - alpha) + heating_rate * alpha

                # Bound heating_power to realistic values for residential heating systems
                clamped_power = max(
                    MIN_HEATING_POWER, min(MAX_HEATING_POWER, unbounded_new_power)
                )
                if clamped_power != unbounded_new_power:
                    bound_name = (
                        "MIN_HEATING_POWER"
                        if clamped_power <= MIN_HEATING_POWER
                        else "MAX_HEATING_POWER"
                    )
                    _LOGGER.debug(
                        "better_thermostat: heating_power clamped from %.4f to %.4f at %s "
                        "(min=%.4f, max=%.4f)",
                        unbounded_new_power,
                        clamped_power,
                        bound_name,
                        MIN_HEATING_POWER,
                        MAX_HEATING_POWER,
                    )
                new_power = clamped_power
                self.heating_power = round(new_power, 4)
                heating_power_changed = self.heating_power != old_power

                # Compact short stats history
                self.last_heating_power_stats.append(
                    {
                        "dT": round(temp_diff, 2),
                        "min": round(duration_min, 1),
                        "rate": heating_rate,
                        "alpha": round(alpha, 3),
                        "envf": round(env_factor, 3),
                        "hp": self.heating_power,
                        "norm": normalized_power,
                    }
                )

                # Full cycle telemetry snapshot (bounded deque)
                try:
                    self.heating_cycles.append(
                        {
                            "start": (
                                self.heating_start_timestamp.isoformat()
                                if self.heating_start_timestamp
                                else None
                            ),
                            "end": (
                                self.heating_end_timestamp.isoformat()
                                if self.heating_end_timestamp
                                else None
                            ),
                            "temp_start": (
                                round(self.heating_start_temp, 2)
                                if self.heating_start_temp is not None
                                else None
                            ),
                            "temp_peak": (
                                round(self.heating_end_temp, 2)
                                if self.heating_end_temp is not None
                                else None
                            ),
                            "delta_t": round(temp_diff, 3),
                            "minutes": round(duration_min, 2),
                            "rate_c_min": heating_rate,
                            "target": self.bt_target_temp,
                            "outdoor": outdoor,
                            "norm_power": normalized_power,
                        }
                    )
                except Exception:
                    _LOGGER.exception(
                        "Error appending heating cycle telemetry snapshot"
                    )

                _LOGGER.debug(
                    "better_thermostat %s: heating cycle evaluated: ΔT=%.3f°C, t=%.2fmin, rate=%.4f°C/min, " \
                    "hp(old/new)=%.4f/%.4f, alpha=%.3f, env_factor=%.3f, norm=%s",
                    self.device_name,
                    temp_diff,
                    duration_min,
                    heating_rate,
                    old_power,
                    self.heating_power,
                    alpha,
                    env_factor,
                    normalized_power,
                )

            # Reset for next cycle (even if discarded)
            self.heating_start_temp = None
            self.heating_end_temp = None
            self.heating_start_timestamp = None
            self.heating_end_timestamp = None

        # Adjust dynamic target range bounds based on used setpoints
        if self.bt_target_temp is not None:
            self.min_target_temp = min(self.min_target_temp, self.bt_target_temp)
            self.max_target_temp = max(self.max_target_temp, self.bt_target_temp)

        # Track action changes using freshly computed action (pure function)
        if action_changed:
            self.old_attr_hvac_action = current_action
            self.attr_hvac_action = (
                current_action  # maintain legacy attribute for compatibility
            )

        # Write state only if something relevant changed
        if heating_power_changed or action_changed or finalize:
            # Store normalized power if available
            if normalized_power is not None:
                self.heating_power_normalized = normalized_power
            if heating_power_changed:
                self._schedule_save_thermal_stats()
            self.async_write_ha_state()

    async def calculate_heat_loss(self):
        """Learn effective heat loss (°C/min) during idle cooling periods.

        Measures temperature decay when HVAC action is IDLE and the window is closed.
        Similar to heating_power, but for passive cooling (loss rate).
        """

        if self.cur_temp is None:
            return

        now = dt_util.utcnow()
        current_action = self._compute_hvac_action()

        # Do not learn when window is open
        if self.window_open:
            self.loss_start_temp = None
            self.loss_start_timestamp = None
            self.loss_end_temp = None
            self.loss_end_timestamp = None
            self._loss_last_action = current_action
            return

        # Start tracking when we enter idle (not heating)
        if current_action != HVACAction.HEATING:
            if self.loss_start_temp is None:
                self.loss_start_temp = self.cur_temp
                self.loss_start_timestamp = now
                self.loss_end_temp = self.cur_temp
                self.loss_end_timestamp = now
            elif self.loss_end_temp is None or self.cur_temp < self.loss_end_temp:
                self.loss_end_temp = self.cur_temp
                self.loss_end_timestamp = now

        # Finalize when heating starts again
        if current_action == HVACAction.HEATING and self.loss_start_temp is not None:
            if self.loss_end_temp is not None and self.loss_start_timestamp is not None:
                temp_drop = self.loss_start_temp - self.loss_end_temp
                duration_min = (
                    (
                        self.loss_end_timestamp - self.loss_start_timestamp
                    ).total_seconds()
                    / 60.0
                    if self.loss_end_timestamp and self.loss_start_timestamp
                    else 0
                )

                if duration_min >= 1.0 and temp_drop > 0:
                    # Raw loss rate (°C/min)
                    loss_rate = round(temp_drop / duration_min, 5)

                    # Adaptive smoothing
                    base_alpha = 0.10
                    alpha = max(0.02, min(0.25, base_alpha))
                    old_loss = self.heat_loss_rate
                    unbounded = old_loss * (1 - alpha) + loss_rate * alpha

                    clamped_loss = max(MIN_HEAT_LOSS, min(MAX_HEAT_LOSS, unbounded))
                    if clamped_loss != unbounded:
                        bound_name = (
                            "MIN_HEAT_LOSS"
                            if clamped_loss <= MIN_HEAT_LOSS
                            else "MAX_HEAT_LOSS"
                        )
                        _LOGGER.debug(
                            "better_thermostat: heat_loss clamped from %.4f to %.4f at %s "
                            "(min=%.4f, max=%.4f)",
                            unbounded,
                            clamped_loss,
                            bound_name,
                            MIN_HEAT_LOSS,
                            MAX_HEAT_LOSS,
                        )

                    self.heat_loss_rate = round(clamped_loss, 5)
                    loss_changed = self.heat_loss_rate != old_loss

                    self.last_heat_loss_stats.append(
                        {
                            "dT": round(temp_drop, 2),
                            "min": round(duration_min, 1),
                            "rate": loss_rate,
                            "alpha": round(alpha, 3),
                            "loss": self.heat_loss_rate,
                        }
                    )

                    try:
                        self.loss_cycles.append(
                            {
                                "start": (
                                    self.loss_start_timestamp.isoformat()
                                    if self.loss_start_timestamp
                                    else None
                                ),
                                "end": (
                                    self.loss_end_timestamp.isoformat()
                                    if self.loss_end_timestamp
                                    else None
                                ),
                                "temp_start": (
                                    round(self.loss_start_temp, 2)
                                    if self.loss_start_temp is not None
                                    else None
                                ),
                                "temp_min": (
                                    round(self.loss_end_temp, 2)
                                    if self.loss_end_temp is not None
                                    else None
                                ),
                                "rate": loss_rate,
                            }
                        )
                    except Exception:
                        _LOGGER.exception(
                            "better_thermostat %s: Error while storing heat loss cycle",
                            self.device_name,
                        )

                    self.async_write_ha_state()
                    if loss_changed:
                        self._schedule_save_thermal_stats()

            # Reset after finalize
            self.loss_start_temp = None
            self.loss_start_timestamp = None
            self.loss_end_temp = None
            self.loss_end_timestamp = None

        self._loss_last_action = current_action

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device specific state attributes.

        Returns
        -------
        dict
                Attribute dictionary for the extra device specific state attributes.
        """
        dev_specific = {
            ATTR_STATE_WINDOW_OPEN: self.window_open,
            ATTR_STATE_CALL_FOR_HEAT: self.call_for_heat,
            ATTR_STATE_LAST_CHANGE: self.last_change.isoformat(),
            ATTR_STATE_SAVED_TEMPERATURE: self._saved_temperature,
            ATTR_STATE_PRESET_TEMPERATURE: self._preset_temperature,
            ATTR_STATE_HUMIDIY: self._current_humidity,
            ATTR_STATE_MAIN_MODE: self.last_main_hvac_mode,
            ATTR_STATE_OFF_TEMPERATURE: self.off_temperature,
            CONF_TOLERANCE: self.tolerance,
            CONF_TARGET_TEMP_STEP: self.bt_target_temp_step,
            ATTR_STATE_HEATING_POWER: self.heating_power,
            ATTR_STATE_HEAT_LOSS: getattr(self, "heat_loss_rate", None),
            ATTR_STATE_ERRORS: json.dumps(self.devices_errors),
            ATTR_STATE_BATTERIES: json.dumps(self.devices_states),
            "external_temp_ema": self.cur_temp_filtered,
            # Degraded mode: thermostat running with some sensors unavailable
            "degraded_mode": self.degraded_mode,
            "unavailable_sensors": self.unavailable_sensors,
            # ECO mode attribute removed: eco preset supported via PRESET_ECO
        }

        # Optional: next scheduled valve maintenance (ISO8601)
        try:
            if (
                hasattr(self, "next_valve_maintenance")
                and self.next_valve_maintenance is not None
            ):
                dev_specific["next_valve_maintenance"] = (
                    self.next_valve_maintenance.isoformat()
                )
        except Exception:
            pass

        # Optional: summarize last valve method per TRV (adapter vs override)
        try:
            methods = {}
            for trv_id, info in (self.real_trvs or {}).items():
                m = info.get("last_valve_method")
                if m:
                    methods[trv_id] = m
            if methods:
                dev_specific["valve_method"] = methods
        except Exception:
            pass

        # Optional telemetry (memory friendly): only count & last cycle + normalized power
        if hasattr(self, "heating_cycles") and len(self.heating_cycles) > 0:
            last_cycle = self.heating_cycles[-1]
            try:
                dev_specific["heating_cycle_count"] = len(self.heating_cycles)
                dev_specific["heating_cycle_last"] = json.dumps(last_cycle)
            except Exception:
                _LOGGER.exception("Error while serializing heating cycle telemetry")
        if hasattr(self, "loss_cycles") and len(self.loss_cycles) > 0:
            last_cycle = self.loss_cycles[-1]
            try:
                dev_specific["heat_loss_cycle_count"] = len(self.loss_cycles)
                dev_specific["heat_loss_cycle_last"] = json.dumps(last_cycle)
            except Exception:
                _LOGGER.exception("Error while serializing heat loss telemetry")
        if hasattr(self, "last_heat_loss_stats") and self.last_heat_loss_stats:
            try:
                dev_specific[ATTR_STATE_HEAT_LOSS_STATS] = json.dumps(
                    list(self.last_heat_loss_stats)
                )
            except Exception:
                _LOGGER.exception("Error while serializing heat loss stats")
        if hasattr(self, "heating_power_normalized"):
            dev_specific["heating_power_norm"] = getattr(
                self, "heating_power_normalized", None
            )

        # Balance Telemetrie (kompakt)
        if hasattr(self, "temp_slope") and self.temp_slope is not None:
            dev_specific["temp_slope_K_min"] = round(self.temp_slope, 4)
        try:
            # Führe kompakt alle TRV-Balance Infos zusammen (nur valve_percent)
            bal_compact = {}
            for trv, info in self.real_trvs.items():
                bal = info.get("calibration_balance")
                if bal:
                    bal_compact[trv] = {"valve%": bal.get("valve_percent")}
            if bal_compact:
                dev_specific["calibration_balance"] = json.dumps(bal_compact)
        except Exception:
            pass

        # PID/Regler-Debug als flache Attribute für Graphen (nur von repräsentativem TRV)
        try:
            rep_trv = None
            for t in self.real_trvs.keys():
                mdl = str(self.real_trvs.get(t, {}).get("model", ""))
                if "sonoff" in mdl.lower() or "trvzb" in mdl.lower():
                    rep_trv = t
                    break
            if rep_trv is None:
                rep_trv = next(iter(self.real_trvs.keys()), None)
            if rep_trv is not None:
                bal = (self.real_trvs.get(rep_trv, {}) or {}).get(
                    "calibration_balance"
                ) or {}
                dbg = bal.get("debug") or {}
                pid = dbg.get("pid") or {}
                # Nur wenn Modus pid ist, sonst vermeiden wir Rauschen
                if str(pid.get("mode")).lower() == "pid":
                    # Hilfsfunktion: sichere Float-Konvertierung
                    def _to_float(val):
                        try:
                            return float(val)
                        except Exception:
                            return None

                    # Fehler (ΔT), P/I/D/U und Gains direkt ausgeben
                    v = _to_float(pid.get("e_K"))
                    if v is not None:
                        dev_specific["pid_e_K"] = round(v, 4)
                    v = _to_float(pid.get("p"))
                    if v is not None:
                        dev_specific["pid_P"] = round(v, 4)
                    v = _to_float(pid.get("i"))
                    if v is not None:
                        dev_specific["pid_I"] = round(v, 4)
                    v = _to_float(pid.get("d"))
                    if v is not None:
                        dev_specific["pid_D"] = round(v, 4)
                    v = _to_float(pid.get("u"))
                    if v is not None:
                        dev_specific["pid_u"] = round(v, 4)
                    v = _to_float(pid.get("kp"))
                    if v is not None:
                        dev_specific["pid_kp"] = round(v, 6)
                    v = _to_float(pid.get("ki"))
                    if v is not None:
                        dev_specific["pid_ki"] = round(v, 6)
                    v = _to_float(pid.get("kd"))
                    if v is not None:
                        dev_specific["pid_kd"] = round(v, 6)
                    v = _to_float(pid.get("meas_blend_C"))
                    if v is not None:
                        dev_specific["pid_meas_blend_C"] = round(v, 3)
                    v = _to_float(pid.get("meas_smooth_C"))
                    if v is not None:
                        dev_specific["pid_meas_smooth_C"] = round(v, 3)
                    # d_meas_per_s ist K/s; für Lesbarkeit auch auf K/min hochrechnen
                    v = _to_float(pid.get("d_meas_per_s"))
                    if v is not None:
                        dev_specific["pid_d_meas_K_per_min"] = round(v * 60.0, 4)
                    # dt_s
                    v = _to_float(pid.get("dt_s"))
                    if v is not None:
                        dev_specific["pid_dt_s"] = round(v, 3)
        except Exception:
            pass

        return dev_specific

    @property
    def available(self):
        """Return if thermostat is available.

        Returns
        -------
        bool
                True if the thermostat is available.
        """
        return self._available

    @property
    def should_poll(self):
        """Return the polling state.

        Returns
        -------
        bool
                True if the thermostat uses polling.
        """
        return False

    @property
    def unique_id(self):
        """Return the unique id of this thermostat.

        Returns
        -------
        string
                The unique id of this thermostat.
        """
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system.

        Returns
        -------
        float
                Precision of the thermostat.
        """
        return super().precision

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature.

        Returns
        -------
        float
                Step size of target temperature.
        """
        if self.bt_target_temp_step is not None:
            return self.bt_target_temp_step

        return super().precision

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self.cur_temp

    @property
    def current_humidity(self) -> float | None:
        """Return the current humidity if supported."""
        return self._current_humidity if hasattr(self, "_current_humidity") else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current operation."""
        # Fallback if None
        if self.bt_hvac_mode is None:
            return HVACMode.OFF
        mapped = get_hvac_bt_mode(self, self.bt_hvac_mode)
        if isinstance(mapped, HVACMode):
            return mapped
        try:
            return HVACMode(mapped)
        except Exception:
            try:
                return HVACMode[mapped.upper()]
            except Exception:
                return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        return self._hvac_list

    @property
    def hvac_action(self):
        """Return the current HVAC action (delegates to helper)."""
        return self._compute_hvac_action()

    def _should_heat_with_tolerance(
        self, previous_action: HVACAction | None, tol: float
    ) -> bool:
        """Apply hysteresis so heating restarts only below target - tolerance."""
        if self.bt_target_temp is None or self.cur_temp is None:
            return False
        tol = max(0.0, tol)
        heat_off_threshold = self.bt_target_temp
        heat_on_threshold = self.bt_target_temp - tol
        if previous_action == HVACAction.HEATING:
            return self.cur_temp < heat_off_threshold
        return self.cur_temp < heat_on_threshold

    def _compute_hvac_action(self):  # helper kept internal for clarity
        """Pure HVAC action computation with tolerance based hysteresis.

        Rules:
        - OFF mode returns OFF regardless of temperatures
        - Open window suppresses active heating/cooling (returns IDLE)
        - Heating uses a hysteresis band [target - tolerance, target]
        - Cooling if mode heat_cool and cur_temp > cool_target + tolerance
        - Otherwise IDLE, unless TRVs explicitly report heating and tolerance does not block it
        """
        prev_action = self._tolerance_last_action
        tol = self.tolerance if self.tolerance is not None else 0.0

        if self.bt_target_temp is None or self.cur_temp is None:
            self._tolerance_hold_active = False
            self._tolerance_last_action = HVACAction.IDLE
            return HVACAction.IDLE
        if HVACMode.OFF in (self.hvac_mode, self.bt_hvac_mode):
            self._tolerance_hold_active = False
            self._tolerance_last_action = HVACAction.IDLE
            return HVACAction.OFF
        if self.window_open:
            self._tolerance_hold_active = False
            self._tolerance_last_action = HVACAction.IDLE
            return HVACAction.IDLE

        heating_allowed = self.hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL)
        action = HVACAction.IDLE
        tolerance_hold = False

        if heating_allowed:
            should_heat = self._should_heat_with_tolerance(prev_action, tol)
            if should_heat:
                action = HVACAction.HEATING
            else:
                tolerance_hold = True

        # Cooling decision (if heat_cool mode and cooling setpoint exists)
        if (
            self.hvac_mode in (HVACMode.HEAT_COOL,)
            and self.bt_target_cooltemp is not None
            and self.cur_temp > (self.bt_target_cooltemp + tol)
        ):
            action = HVACAction.COOLING
            tolerance_hold = False

        # Base decision would be IDLE. If any real TRV indicates active heating, override to HEATING.
        if action == HVACAction.IDLE:
            try:
                # Skip overrides while we intentionally ignore TRV states or when window is open
                if self.ignore_states or self.window_open:
                    self._tolerance_last_action = HVACAction.IDLE
                    self._tolerance_hold_active = tolerance_hold
                    return HVACAction.IDLE

                def _to_pct(val):
                    try:
                        v = float(val)
                        return v * 100.0 if v <= 1.0 else v
                    except Exception:
                        return None

                THRESH = 0.0
                for trv_id, info in (self.real_trvs or {}).items():
                    if not isinstance(info, dict):
                        _LOGGER.debug(
                            "better_thermostat %s: _compute_hvac_action TRV %s ignored (config invalid)",
                            self.device_name,
                            trv_id,
                        )
                        continue
                    if info.get("ignore_trv_states"):
                        _LOGGER.debug(
                            "better_thermostat %s: _compute_hvac_action TRV %s ignored (ignore_trv_states=True)",
                            self.device_name,
                            trv_id,
                        )
                        continue

                    # 0) Use cached hvac_action first; fallback to hass state if missing
                    try:
                        action_val = info.get("hvac_action")
                        action_str = (
                            str(action_val).lower() if action_val is not None else ""
                        )
                        if not action_str:
                            trv_state = self.hass.states.get(trv_id)
                            action_raw = None
                            if trv_state is not None:
                                action_raw = trv_state.attributes.get("hvac_action")
                                if action_raw is None:
                                    action_raw = trv_state.attributes.get("action")
                            action_str = (
                                str(action_raw).lower()
                                if action_raw is not None
                                else ""
                            )
                            if action_str:
                                try:
                                    info["hvac_action"] = action_str
                                except Exception:
                                    pass
                        if action_str == "heating" or action_val == HVACAction.HEATING:
                            _LOGGER.debug(
                                "better_thermostat %s: overriding hvac_action to HEATING (TRV %s reports heating)",
                                self.device_name,
                                trv_id,
                            )
                            action = HVACAction.HEATING
                            break
                    except Exception:
                        pass

                    # 2) TRV shows an actual/open valve position > 0
                    vp = info.get("valve_position")
                    try:
                        vp_pct = _to_pct(vp)
                        if vp_pct is not None and vp_pct > THRESH:
                            _LOGGER.debug(
                                "better_thermostat %s: overriding hvac_action to HEATING (valve_position %.1f%%, TRV %s)",
                                self.device_name,
                                vp_pct,
                                trv_id,
                            )
                            action = HVACAction.HEATING
                            break
                    except Exception:
                        pass

                    # 3) We last commanded a valve percent > 0 (adapter/override)
                    last_pct = info.get("last_valve_percent")
                    try:
                        last_pct_n = _to_pct(last_pct)
                        if last_pct_n is not None and last_pct_n > THRESH:
                            _LOGGER.debug(
                                "better_thermostat %s: overriding hvac_action to HEATING (last_valve_percent %.1f%%, TRV %s)",
                                self.device_name,
                                last_pct_n,
                                trv_id,
                            )
                            action = HVACAction.HEATING
                            break
                    except Exception:
                        pass

            except Exception:
                # Defensive: if anything goes wrong in overrides, fall back to IDLE
                pass

        # Persist tolerance state machine for next decision
        self._tolerance_last_action = (
            HVACAction.HEATING if action == HVACAction.HEATING else HVACAction.IDLE
        )
        self._tolerance_hold_active = bool(
            tolerance_hold and action != HVACAction.COOLING
        )
        return action

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach.

        Returns
        -------
        float
                Target temperature.
        """
        if self.bt_target_temp is None:
            return None
        if self.bt_min_temp is None or self.bt_max_temp is None:
            return self.bt_target_temp
        # if target temp is below minimum, return minimum
        if self.bt_target_temp < self.bt_min_temp:
            return self.bt_min_temp
        # if target temp is above maximum, return maximum
        if self.bt_target_temp > self.bt_max_temp:
            return self.bt_max_temp
        return self.bt_target_temp

    @property
    def target_temperature_low(self) -> float | None:
        """Return the low target temperature."""
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_temp

    @property
    def target_temperature_high(self) -> float | None:
        """Return the high target temperature."""
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_cooltemp

    async def async_set_hvac_mode(self, hvac_mode: HVACMode | str) -> None:
        """Set hvac mode.

        Returns
        -------
        None
        """

        hvac_mode_norm = normalize_hvac_mode(hvac_mode)
        if hvac_mode_norm in (HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF):
            self.bt_hvac_mode = get_hvac_bt_mode(self, hvac_mode_norm)
        else:
            _LOGGER.error(
                "better_thermostat %s: Unsupported hvac_mode %s",
                self.device_name,
                hvac_mode_norm,
            )
        self.async_write_ha_state()
        # During valve maintenance we must not block on the control queue (maxsize=1)
        # and must not override maintenance valve exercise.
        if getattr(self, "in_maintenance", False):
            self._control_needed_after_maintenance = True
            return

        await self.control_queue_task.put(self)

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        _LOGGER.debug(
            "better_thermostat %s: async_set_temperature kwargs=%s, current preset=%s, hvac_mode=%s",
            self.device_name,
            kwargs,
            self._preset_mode,
            self.bt_hvac_mode,
        )

        _new_setpoint = None
        _new_setpointlow = None
        _new_setpointhigh = None

        if ATTR_HVAC_MODE in kwargs:
            hvac_mode_val = kwargs.get(ATTR_HVAC_MODE, None)
            hvac_mode_norm = (
                normalize_hvac_mode(hvac_mode_val)
                if hvac_mode_val is not None
                else None
            )
            if hvac_mode_norm in (HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.OFF):
                self.bt_hvac_mode = hvac_mode_norm
            else:
                _LOGGER.error(
                    "better_thermostat %s: Unsupported hvac_mode %s",
                    self.device_name,
                    hvac_mode_norm,
                )
        if ATTR_TEMPERATURE in kwargs:
            _new_setpoint = convert_to_float(
                str(kwargs.get(ATTR_TEMPERATURE, None)),
                self.device_name,
                "controlling.settarget_temperature()",
            )

        if ATTR_TARGET_TEMP_LOW in kwargs:
            _new_setpointlow = convert_to_float(
                str(kwargs.get(ATTR_TARGET_TEMP_LOW, None)),
                self.device_name,
                "controlling.settarget_temperature_low()",
            )

        if ATTR_TARGET_TEMP_HIGH in kwargs:
            _new_setpointhigh = convert_to_float(
                str(kwargs.get(ATTR_TARGET_TEMP_HIGH, None)),
                self.device_name,
                "controlling.settarget_temperature_high()",
            )

        if _new_setpoint is None and _new_setpointlow is None:
            _LOGGER.debug(
                "better_thermostat %s: async_set_temperature kwargs=%s, current preset=%s, hvac_mode=%s",
                self.device_name,
                kwargs,
                self._preset_mode,
                self.bt_hvac_mode,
            )

            _new_setpoint = None
            _new_setpointlow = None
            _new_setpointhigh = None

        if _new_setpointhigh is not None:
            self.bt_target_cooltemp = _new_setpointhigh

        # Enforce ordering: cool target should be above heat target (if both in heat_cool mode)
        if (
            self.hvac_mode in (HVACMode.HEAT_COOL,)
            and self.bt_target_cooltemp is not None
            and self.bt_target_temp is not None
            and self.bt_target_cooltemp <= self.bt_target_temp
        ):
            step = self.bt_target_temp_step or 0.5
            adjusted = self.bt_target_temp + step
            _LOGGER.warning(
                "better_thermostat %s: cooling target %.2f adjusted to %.2f to stay above heating target %.2f",
                self.device_name,
                self.bt_target_cooltemp,
                adjusted,
                self.bt_target_temp,
            )
            self.bt_target_cooltemp = adjusted

        # If user manually changes the temperature while a preset is active,
        # update the stored preset temperature so that returning to the preset
        # later reuses the newly chosen value instead of the originally
        # configured one. This applies to ALL presets including PRESET_NONE.
        # Note: We still avoid persisting to config entry options here to
        # prevent frequent integration reloads; persistence can be handled
        # via state restore or an explicit save action.
        if self._preset_mode in self._preset_temperatures and (
            _new_setpoint is not None or _new_setpointlow is not None
        ):
            # Only update stored preset temperature for PRESET_NONE (Manual)
            # For other presets, the value is controlled via Number entities.
            if self._preset_mode == PRESET_NONE:
                if self.bt_target_temp is not None:
                    applied = float(self.bt_target_temp)
                    old_value = self._preset_temperatures.get(self._preset_mode)
                    if old_value != applied:
                        self._preset_temperatures[self._preset_mode] = applied
                        _LOGGER.debug(
                            "better_thermostat %s: Updated stored preset temperature for %s from %s to %s due to manual change",
                            self.device_name,
                            self._preset_mode,
                            old_value,
                            applied,
                        )

            if ATTR_TEMPERATURE in kwargs:
                _new_setpoint = convert_to_float(
                    str(kwargs.get(ATTR_TEMPERATURE, None)),
                    self.device_name,
                    "controlling.settarget_temperature()",
                )

            if ATTR_TARGET_TEMP_LOW in kwargs:
                _new_setpointlow = convert_to_float(
                    str(kwargs.get(ATTR_TARGET_TEMP_LOW, None)),
                    self.device_name,
                    "controlling.settarget_temperature_low()",
                )

            if ATTR_TARGET_TEMP_HIGH in kwargs:
                _new_setpointhigh = convert_to_float(
                    str(kwargs.get(ATTR_TARGET_TEMP_HIGH, None)),
                    self.device_name,
                    "controlling.settarget_temperature_high()",
                )

            if _new_setpoint is None and _new_setpointlow is None:
                _LOGGER.debug(
                    "better_thermostat %s: received a new setpoint from HA, but temperature attribute was not set, ignoring",
                    self.device_name,
                )
                return

            # Validate against min/max temps
            if _new_setpoint is not None:
                _new_setpoint = min(self.max_temp, max(self.min_temp, _new_setpoint))
            if _new_setpointlow is not None:
                _new_setpointlow = min(
                    self.max_temp, max(self.min_temp, _new_setpointlow)
                )
            if _new_setpointhigh is not None:
                _new_setpointhigh = min(
                    self.max_temp, max(self.min_temp, _new_setpointhigh)
                )

            # Preserve explicit 0.0 values (avoid Python truthiness bug)
            if _new_setpoint is not None:
                self.bt_target_temp = _new_setpoint
            else:
                self.bt_target_temp = _new_setpointlow

            if _new_setpointhigh is not None:
                self.bt_target_cooltemp = _new_setpointhigh

            # Enforce ordering: cool target should be above heat target (if both in heat_cool mode)
            if (
                self.hvac_mode in (HVACMode.HEAT_COOL,)
                and self.bt_target_cooltemp is not None
                and self.bt_target_temp is not None
                and self.bt_target_cooltemp <= self.bt_target_temp
            ):
                step = self.bt_target_temp_step or 0.5
                adjusted = self.bt_target_temp + step
                _LOGGER.warning(
                    "better_thermostat %s: cooling target %.2f adjusted to %.2f to stay above heating target %.2f",
                    self.device_name,
                    self.bt_target_cooltemp,
                    adjusted,
                    self.bt_target_temp,
                )
                self.bt_target_cooltemp = adjusted

            # If user manually changes the temperature while a preset is active,
            # we ONLY update the stored preset temperature if we are in PRESET_NONE (Manual).
            # For specific presets (Comfort, Eco, etc.), the value is now managed via
            # separate Number entities and should NOT be overwritten by manual setpoint changes.
            if (
                self._preset_mode == PRESET_NONE
                and self._preset_mode in self._preset_temperatures
                and (_new_setpoint is not None or _new_setpointlow is not None)
            ):
                if self.bt_target_temp is not None:
                    applied = float(self.bt_target_temp)
                    old_value = self._preset_temperatures.get(self._preset_mode)
                    if old_value != applied:
                        self._preset_temperatures[self._preset_mode] = applied
                        _LOGGER.debug(
                            "better_thermostat %s: Updated stored preset temperature for %s from %s to %s due to manual change",
                            self.device_name,
                            self._preset_mode,
                            old_value,
                            applied,
                        )
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: Manual change equals current stored preset %s value=%s; no update",
                            self.device_name,
                            self._preset_mode,
                            applied,
                        )

            # Enforce ordering: cool target should be above heat target (if both in heat_cool mode)
            if (
                self.hvac_mode in (HVACMode.HEAT_COOL,)
                and self.bt_target_cooltemp is not None
                and self.bt_target_temp is not None
                and self.bt_target_cooltemp <= self.bt_target_temp
            ):
                step = self.bt_target_temp_step or 0.5
                adjusted = self.bt_target_temp + step
                _LOGGER.warning(
                    "better_thermostat %s: cooling target %.2f adjusted to %.2f to stay above heating target %.2f",
                    self.device_name,
                    self.bt_target_cooltemp,
                    adjusted,
                    self.bt_target_temp,
                )
                self.bt_target_cooltemp = adjusted

            _LOGGER.debug(
                "better_thermostat %s: HA set target temperature to %s & %s",
                self.device_name,
                self.bt_target_temp,
                self.bt_target_cooltemp,
            )

            self.async_write_ha_state()
            # Only trigger control queue if thermostat is not OFF
            # When OFF, we still save the temperature but don't send it to the physical device
            if self.bt_hvac_mode != HVACMode.OFF:
                # During valve maintenance we must not block on the control queue
                # (Queue maxsize=1) and must not override maintenance.
                if getattr(self, "in_maintenance", False):
                    self._control_needed_after_maintenance = True
                    return
                await self.control_queue_task.put(self)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    def _signal_config_change(self) -> None:
        """Signal a configuration change to trigger entity cleanup/recreation."""
        signal_key = f"bt_config_changed_{self._config_entry_id}"
        dispatcher_send(self.hass, signal_key, {"entry_id": self._config_entry_id})
        _LOGGER.debug(
            "better_thermostat %s: Signaled configuration change",
            self.device_name,
        )

    async def run_valve_maintenance_service(self) -> None:
        """Entity service: run valve maintenance immediately (ignores schedule)."""
        try:
            if self.in_maintenance:
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance already running",
                    self.device_name,
                )
                return
            # gather enabled TRVs
            trvs_to_service = [
                trv_id
                for trv_id, info in self.real_trvs.items()
                if bool(
                    (info.get("advanced", {}) or {}).get(CONF_VALVE_MAINTENANCE, False)
                )
            ]
            if not trvs_to_service:
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance requested, but no TRV has it enabled",
                    self.device_name,
                )
                return
            # force immediate run
            self.next_valve_maintenance = datetime.now()
            await self._run_valve_maintenance(trvs_to_service)
        except Exception:
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance service encountered an error",
                self.device_name,
            )

    @property
    def min_temp(self):
        """Return the minimum temperature.

        Returns
        -------
        float
                the minimum temperature.
        """
        if self.bt_min_temp is not None:
            return self.bt_min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature.

        Returns
        -------
        float
                the maximum temperature.
        """
        if self.bt_max_temp is not None:
            return self.bt_max_temp

        # Get default temp from super class
        return super().max_temp

    @property
    def _is_device_active(self):
        """Get the current state of the device for HA.

        Returns
        -------
        string
                State of the device.
        """
        if self.bt_hvac_mode == HVACMode.OFF:
            return False
        if self.window_open:
            return False
        return True

    @property
    def supported_features(self):
        """Return the list of supported features.

        Returns
        -------
        array
                Supported features.
        """
        if self.cooler_entity_id is not None:
            return (
                ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
                | ClimateEntityFeature.PRESET_MODE
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.TURN_ON
            )
        return (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
        )

    @property
    def preset_mode(self):
        """Return the current preset mode."""
        return self._preset_mode

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode (HA async API).

        NOTE:
            Home Assistant calls `async_set_preset_mode` directly when present.
            Previously this integration implemented an async coroutine named
            `set_preset_mode` (without the `async_` prefix). The core will
            assume a method named `set_preset_mode` is synchronous and will try
            to execute it inside an executor thread. Because it was actually
            declared with `async def`, HA attempted to run a coroutine function
            via `run_in_executor`, resulting in an error similar to:

                "set_preset_mode cannot be used with run_in_executor".

            Renaming the method to `async_set_preset_mode` fixes this by letting
            HA await the coroutine directly.
        """
        if preset_mode not in self.preset_modes:
            _LOGGER.warning(
                "better_thermostat %s: Unsupported preset mode %s",
                self.device_name,
                preset_mode,
            )
            return

        self.bt_update_lock = True
        try:
            old_preset = self._preset_mode
            self._preset_mode = preset_mode

            _LOGGER.debug(
                "better_thermostat %s: Setting preset mode from %s to %s",
                self.device_name,
                old_preset,
                preset_mode,
            )

            # If switching from PRESET_NONE to another preset, save current temperature
            if old_preset == PRESET_NONE and preset_mode != PRESET_NONE:
                if self._preset_temperature is None:
                    self._preset_temperature = self.bt_target_temp
                    _LOGGER.debug(
                        "better_thermostat %s: Saved temperature %s before entering preset mode",
                        self.device_name,
                        self._preset_temperature,
                    )

            # If switching back to PRESET_NONE, restore saved temperature
            if preset_mode == PRESET_NONE and self._preset_temperature is not None:
                self.bt_target_temp = self._preset_temperature
                self._preset_temperature = None
                _LOGGER.debug(
                    "better_thermostat %s: Restored temperature %s from preset mode",
                    self.device_name,
                    self.bt_target_temp,
                )

            # Apply configured preset temperature
            elif (
                preset_mode != PRESET_NONE and preset_mode in self._preset_temperatures
            ):
                # Use the configured absolute temperature for this preset
                configured_temp = self._preset_temperatures[preset_mode]

                _LOGGER.debug(
                    "better_thermostat %s: Preset %s configured: %.1f, Min: %.1f, Max: %.1f",
                    self.device_name,
                    preset_mode,
                    configured_temp,
                    self.min_temp,
                    self.max_temp,
                )

                # Ensure the temperature is within min/max bounds
                new_temp = min(self.max_temp, max(self.min_temp, configured_temp))

                self.bt_target_temp = new_temp
                _LOGGER.debug(
                    "better_thermostat %s: Applied preset %s with configured temperature: %s°C",
                    self.device_name,
                    preset_mode,
                    new_temp,
                )

            _LOGGER.debug(
                "better_thermostat %s: After preset change %s -> %s, bt_target_temp=%s, bt_hvac_mode=%s",
                self.device_name,
                old_preset,
                preset_mode,
                self.bt_target_temp,
                self.bt_hvac_mode,
            )

            self.async_write_ha_state()
            if (
                hasattr(self, "control_queue_task")
                and self.control_queue_task is not None
            ):
                await self.control_queue_task.put(self)
        finally:
            self.bt_update_lock = False

    # Backwards compatibility: If anything external still tries to call the old
    # (incorrect) async method name, provide a thin wrapper. This is intentionally
    # NOT async so HA will not pick it up as the implementation again.
    # type: ignore[override] # Backward compatibility wrapper
    def set_preset_mode(self, preset_mode: str) -> None:
        """Backward compatible wrapper.

        This wrapper schedules the new async method on the event loop. It should
        only be hit by external/custom code; HA core will prefer async_set_preset_mode.
        """
        if self.hass is None:
            return
        # Schedule without waiting; state updates will propagate asynchronously.
        self.hass.async_create_task(self.async_set_preset_mode(preset_mode))

    @property
    def preset_modes(self):
        """Return the available preset modes."""
        return [PRESET_NONE] + self._enabled_presets

    async def reset_pid_learnings_service(
        self,
        apply_pid_defaults: bool = False,
        defaults_kp: float | None = None,
        defaults_ki: float | None = None,
        defaults_kd: float | None = None,
    ) -> None:
        """Entity service: reset learned PID state for this entity.

        - Clears all cached PIDState entries for this entity (all TRVs/buckets)
        - Schedules persistence saves for the map
        """
        try:
            prefix = f"{self._unique_id}:"
            # Collect keys to reset from balance module
            current = pid_export_states(prefix=prefix) or {}
            count = 0
            for key in list(current.keys()):
                try:
                    pid_reset_state(key)
                    count += 1
                except Exception:
                    pass
            _LOGGER.info(
                "better_thermostat %s: reset %d PID learning state entries (prefix=%s)",
                self.device_name,
                count,
                prefix,
            )
            # Schedule persistence of cleared PID states
            try:
                self.schedule_save_pid_state()
            except Exception:
                pass

            # Optionally seed PID defaults for the CURRENT target bucket(s)
            if apply_pid_defaults:
                try:
                    from .utils.calibration.pid import PIDParams, seed_pid_gains

                    # Use provided overrides or PIDParams defaults
                    _defs = PIDParams()
                    kp = float(defaults_kp) if defaults_kp is not None else _defs.kp
                    ki = float(defaults_ki) if defaults_ki is not None else _defs.ki
                    kd = float(defaults_kd) if defaults_kd is not None else _defs.kd

                    # Build current bucket tag based on current heat target
                    def _bucket(temp):
                        try:
                            return f"t{round(float(temp) * 2.0) / 2.0:.1f}"
                        except Exception:
                            return None

                    # Build list of candidate buckets: current and ±0.5°C neighbors
                    bucket_tag = _bucket(self.bt_target_temp)
                    buckets: list[str] = []
                    try:
                        if isinstance(self.bt_target_temp, (int, float)):
                            base = round(float(self.bt_target_temp) * 2.0) / 2.0
                            buckets = [
                                f"t{base:.1f}",
                                f"t{base + 0.5:.1f}",
                                f"t{base - 0.5:.1f}",
                            ]
                        elif bucket_tag:
                            buckets = [bucket_tag]
                    except Exception:
                        if bucket_tag:
                            buckets = [bucket_tag]
                    uid = self.unique_id or self._unique_id or "bt"
                    seeded = 0
                    for trv_id in self.real_trvs.keys():
                        for b in buckets or []:
                            key = f"{uid}:{trv_id}:{b}"
                            try:
                                if seed_pid_gains(key, kp=kp, ki=ki, kd=kd):
                                    seeded += 1
                            except Exception:
                                pass
                    if seeded > 0:
                        _LOGGER.info(
                            "better_thermostat %s: applied PID defaults (kp=%.3f ki=%.3f kd=%.3f) to %d bucket state(s) across %d TRV(s)",
                            self.device_name,
                            kp,
                            ki,
                            kd,
                            seeded,
                            len(list(self.real_trvs.keys()) or []),
                        )
                        try:
                            self.schedule_save_pid_state()
                        except Exception:
                            pass
                        # Kick the control loop so the new gains are used promptly
                        try:
                            await self.control_queue_task.put(self)
                        except Exception:
                            pass
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: apply_pid_defaults did not seed any bucket (bt_target_temp=%s, buckets=%s)",
                            self.device_name,
                            self.bt_target_temp,
                            buckets,
                        )
                except Exception as e:
                    _LOGGER.debug(
                        "better_thermostat %s: apply_pid_defaults failed: %s",
                        self.device_name,
                        e,
                    )
        except Exception as e:
            _LOGGER.debug(
                "better_thermostat %s: reset_pid_learnings_service error: %s",
                self.device_name,
                e,
            )

    async def _async_update_ema_periodic(self, now=None):
        """Periodically update the EMA filter to ensure it converges even if sensor is silent."""
        # Skip if startup is still running to avoid race conditions or confusing logs
        if self.startup_running:
            return

        from .events.temperature import _update_external_temp_ema

        _LOGGER.debug(
            "better_thermostat %s: _async_update_ema_periodic triggered",
            self.device_name,
        )

        last_raw = self.last_known_external_temp
        if last_raw is not None:
            try:
                _LOGGER.debug(
                    "better_thermostat %s: updating EMA with last_raw=%s",
                    self.device_name,
                    last_raw,
                )

                # Calculate slope from EMA change
                old_ema = self.external_temp_ema
                old_ts = self._slope_periodic_last_ts
                now_ts = monotonic()

                new_ema = _update_external_temp_ema(self, float(last_raw))

                if old_ema is not None and old_ts is not None:
                    dt_min = (now_ts - old_ts) / 60.0
                    if dt_min > 0.1:  # Avoid division by zero or tiny steps
                        delta_T = new_ema - old_ema
                        slope = delta_T / dt_min
                        self.temp_slope = slope
                        _LOGGER.debug(
                            "better_thermostat %s: periodic slope calc: old_ema=%.3f new_ema=%.3f dt=%.2fmin -> slope=%.4f K/min",
                            self.device_name,
                            old_ema,
                            new_ema,
                            dt_min,
                            slope,
                        )

                self._slope_periodic_last_ts = now_ts

                _LOGGER.debug(
                    "better_thermostat %s: periodic EMA result=%.3f",
                    self.device_name,
                    new_ema,
                )
                # If the sensor entity is listening to state changes, we should trigger an update
                # But we don't want to spam the state machine if nothing changed significantly?
                # The sensor entity reads `cur_temp_filtered` from `self`.
                # We can just write state if we want the sensor to update.
                # But `async_write_ha_state` updates the climate entity state.
                # The sensor listens to the climate entity.
                # So we should call `async_write_ha_state` if we want the sensor to see the new EMA.
                self.async_write_ha_state()
            except Exception as e:
                _LOGGER.error(
                    "better_thermostat %s: error in _async_update_ema_periodic: %s",
                    self.device_name,
                    e,
                )
        else:
            _LOGGER.debug(
                "better_thermostat %s: _async_update_ema_periodic skipped (no last_known_external_temp)",
                self.device_name,
            )

    async def async_will_remove_from_hass(self):
        """Run when entity will be removed from hass."""
        if self._control_task:
            self._control_task.cancel()
            try:
                await self._control_task
            except asyncio.CancelledError:
                pass
        if self._window_task:
            self._window_task.cancel()
            try:
                await self._window_task
            except asyncio.CancelledError:
                pass
        await super().async_will_remove_from_hass()
