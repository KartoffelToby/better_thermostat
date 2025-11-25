"""Better Thermostat"""

import asyncio
import json
import logging
from abc import ABC
from datetime import datetime, timedelta
from random import randint
from statistics import mean

# preferred for HA time handling (UTC aware)
from homeassistant.util import dt as dt_util
from collections import deque
from typing import Any, Optional

# Home Assistant imports
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    PRESET_NONE,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_SLEEP,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_ACTIVITY,
    PRESET_HOME,
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

# Local imports
from .adapters.delegate import (
    get_current_offset,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    load_adapter,
    set_temperature as adapter_set_temperature,
    set_hvac_mode as adapter_set_hvac_mode,
    # set_valve as adapter_set_valve,  # removed (unused)
)
from .events.cooler import trigger_cooler_change
from .events.temperature import trigger_temperature_change
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue
from .model_fixes.model_quirks import load_model_quirks
from .utils.const import (
    ATTR_STATE_BATTERIES,
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_ERRORS,
    ATTR_STATE_HEATING_POWER,
    ATTR_STATE_HUMIDIY,
    ATTR_STATE_LAST_CHANGE,
    ATTR_STATE_MAIN_MODE,
    ATTR_STATE_SAVED_TEMPERATURE,
    ATTR_STATE_PRESET_TEMPERATURE,
    ATTR_STATE_WINDOW_OPEN,
    BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
    CONF_COOLER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_TARGET_TEMP_STEP,
    CONF_TOLERANCE,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESET_PID_LEARNINGS,
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE,
    SERVICE_SET_TEMP_TARGET_TEMPERATURE,
    SERVICE_START_MPC_DEADZONE_CALIBRATION,
    BETTERTHERMOSTAT_RESET_PID_SCHEMA,
    SUPPORT_FLAGS,
    VERSION,
)
from .utils.controlling import control_queue, control_trv
from .utils.helpers import convert_to_float, find_battery_entity, get_hvac_bt_mode
from .utils.watcher import check_all_entities
from .utils.weather import check_ambient_air_temperature, check_weather
from .utils.helpers import normalize_hvac_mode, get_device_model
from .balance import (
    export_states as balance_export_states,
    import_states as balance_import_states,
    reset_balance_state as balance_reset_state,
)


_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"


class ContinueLoop(Exception):
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


async def async_setup_platform(
    hass, config, async_add_entities, discovery_info=None
):  # noqa: D401
    """(Deprecated) Set up the Better Thermostat platform (no-op)."""
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
    platform.async_register_entity_service(
        SERVICE_START_MPC_DEADZONE_CALIBRATION,
        {},
        "start_mpc_deadzone_calibration_service",
    )

    async_add_entities(
        [
            BetterThermostat(
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
                hass.config.units.temperature_unit,
                entry.entry_id,
                device_class="better_thermostat",
                state_class="better_thermostat_state",
            )
        ]
    )
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
        if self._saved_temperature is None:
            self._saved_temperature = self.bt_target_temp
            self.bt_target_temp = convert_to_float(
                temperature, self.device_name, "service.set_temp_temperature()"
            )
            self.async_write_ha_state()
            await self.control_queue_task.put(self)
        else:
            self.bt_target_temp = convert_to_float(
                temperature, self.device_name, "service.set_temp_temperature()"
            )
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def restore_temp_temperature(self):
        """Restore the previously saved target temperature."""
        if self._saved_temperature is not None:
            self.bt_target_temp = convert_to_float(
                self._saved_temperature,
                self.device_name,
                "service.restore_temp_temperature()",
            )
            self._saved_temperature = None
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def reset_heating_power(self):
        """Reset heating power to default value."""
        self.heating_power = 0.01
        self.async_write_ha_state()

    @property
    def device_info(self):
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
        self.humidity_entity_id = humidity_sensor_entity_id
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
            except (TypeError, ValueError):  # noqa: BLE001
                _LOGGER.warning(
                    "better_thermostat %s: invalid off_temperature '%s', ignoring",
                    self.device_name,
                    off_temperature,
                )
        # Robust tolerance parsing & sanitizing
        try:
            self.tolerance = float(tolerance) if tolerance is not None else 0.0
        except (TypeError, ValueError):  # noqa: BLE001
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
        self.startup_running = True
        self._saved_temperature = None
        self._preset_temperature = (
            None  # Temperature saved before entering any preset mode
        )
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
        self.heating_start_temp = None
        self.heating_start_timestamp = None
        self.heating_end_temp = None
        self.heating_end_timestamp = None
        self._async_unsub_state_changed = None
        self.all_entities = []
        self.devices_states = {}
        self.devices_errors = []
        self.control_queue_task = asyncio.Queue(maxsize=1)
        if self.window_id is not None:
            self.window_queue_task = asyncio.Queue(maxsize=1)
        asyncio.create_task(control_queue(self))
        if self.window_id is not None:
            asyncio.create_task(window_queue(self))
        self.heating_power = 0.01
        # Short bounded history of recent heating power evaluations
        self.last_heating_power_stats = deque(maxlen=10)
        self.is_removed = False
        # Valve maintenance control
        self.in_maintenance = False
        # Balance / Hydraulic: temperature trend (K/min)
        self.temp_slope = None
        self._slope_last_temp = None
        self._slope_last_ts = None
        # Persistence for balance (hydraulic) states
        self._balance_store = None
        self._balance_save_scheduled = False
        # Learned Sonoff/TRV open caps (min/max %) per TRV and target temp bucket
        self.open_caps = {}
        self._open_caps_store = None
        self._open_caps_save_scheduled = False

    async def async_added_to_hass(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        if isinstance(self.all_trvs, str):
            return _LOGGER.error(
                "You updated from version before 1.0.0-Beta36 of the Better Thermostat integration, you need to remove the BT devices (integration) and add it again."
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
            if _calibration_type == "local_calibration_based":
                _calibration = 0
            if _calibration_type == "hybrid_calibration":
                _calibration = 2
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
            except Exception as e:
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
            except Exception as e:  # noqa: BLE001
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

        self.async_on_remove(on_remove)

        await super().async_added_to_hass()

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.device_name
        )

        # Initialize persistent storage for balance states and attempt to load
        try:
            self._balance_store = Store(self.hass, 1, f"{DOMAIN}_balance_states")
            await self._load_balance_state()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug(
                "better_thermostat %s: balance storage init/load failed: %s",
                self.device_name,
                e,
            )

        # Initialize persistent storage for learned open caps
        try:
            self._open_caps_store = Store(self.hass, 1, f"{DOMAIN}_open_caps")
            await self._load_open_caps()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug(
                "better_thermostat %s: open caps storage init/load failed: %s",
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
        _check = await check_all_entities(self)
        if _check is False:
            return
        await check_weather(self)
        if self._last_call_for_heat != self.call_for_heat:
            self._last_call_for_heat = self.call_for_heat
            await self.async_update_ha_state(force_refresh=True)
            self.async_write_ha_state()
            if event is not None:
                await self.control_queue_task.put(self)

    async def _trigger_time(self, event=None):
        _check = await check_all_entities(self)
        if _check is False:
            return
        _LOGGER.debug(
            "better_thermostat %s: get last avg outdoor temps...", self.device_name
        )
        await check_ambient_air_temperature(self)
        self.async_write_ha_state()
        if event is not None:
            await self.control_queue_task.put(self)

    async def _trigger_temperature_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self.hass.async_create_task(trigger_temperature_change(self, event))

    async def _external_temperature_keepalive(self, event=None):
        """Re-send the external temperature regularly to the TRVs.
        Many devices expect an update at least every ~30 minutes."""
        try:
            cur = getattr(self, "cur_temp", None)
            if cur is None:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive skipped (cur_temp is None)",
                    getattr(self, "device_name", "unknown"),
                )
                return

            # Verwende die bekannten TRV-Entity-IDs (Keys in real_trvs)
            trv_ids = list(getattr(self, "real_trvs", {}).keys())
            # Fallback (sollte i.d.R. nicht benötigt werden)
            if not trv_ids and hasattr(self, "entity_ids"):
                trv_ids = list(getattr(self, "entity_ids", []) or [])
            if not trv_ids and hasattr(self, "heater_entity_id"):
                trv_ids = [self.heater_entity_id]
            if not trv_ids:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive: no TRVs found",
                    getattr(self, "device_name", "unknown"),
                )
                return
            else:
                _LOGGER.debug(
                    "better_thermostat %s: external_temperature keepalive: %d TRV(s) found",
                    getattr(self, "device_name", "unknown"),
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
                            getattr(self, "device_name", "unknown"),
                            trv_id,
                        )
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "better_thermostat %s: external_temperature keepalive write failed for %s (non critical)",
                        getattr(self, "device_name", "unknown"),
                        trv_id,
                    )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "better_thermostat %s: external_temperature keepalive encountered an error",
                getattr(self, "device_name", "unknown"),
            )

    async def _trigger_humidity_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self._current_humidity = convert_to_float(
            str(self.hass.states.get(self.humidity_entity_id).state),
            self.device_name,
            "humidity_update",
        )
        self.async_write_ha_state()

    async def _trigger_trv_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if self._async_unsub_state_changed is None:
            return

        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_trv_change(self, event))

    async def _trigger_window_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_window_change(self, event))

    async def _tigger_cooler_change(self, event):
        _check = await check_all_entities(self)
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(trigger_cooler_change(self, event))

    async def startup(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        while self.startup_running:
            _LOGGER.info(
                "better_thermostat %s: Starting version %s. Waiting for entity to be ready...",
                self.device_name,
                self.version,
            )

            sensor_state = self.hass.states.get(self.sensor_entity_id)

            try:
                for trv in self.real_trvs.keys():
                    trv_state = self.hass.states.get(trv)
                    if trv_state is None:
                        _LOGGER.info(
                            "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                            self.device_name,
                            trv,
                        )
                        await asyncio.sleep(10)
                        raise ContinueLoop
                    if trv_state is not None:
                        if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                            _LOGGER.info(
                                "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                                self.device_name,
                                trv,
                            )
                            await asyncio.sleep(10)
                            raise ContinueLoop
            except ContinueLoop:
                continue

            if self.window_id is not None:
                if self.hass.states.get(self.window_id).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for window sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.window_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.cooler_entity_id is not None:
                if self.hass.states.get(self.cooler_entity_id).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for cooler entity with id '%s' to become fully available...",
                        self.device_name,
                        self.cooler_entity_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.humidity_entity_id is not None:
                humidity_state = self.hass.states.get(self.humidity_entity_id)
                if humidity_state is None or humidity_state.state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for humidity sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.humidity_entity_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.outdoor_sensor is not None:
                if self.hass.states.get(self.outdoor_sensor).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for outdoor sensor entity with id '%s' to become fully available...",
                        self.device_name,
                        self.outdoor_sensor,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.weather_entity is not None:
                if self.hass.states.get(self.weather_entity).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for weather entity with id '%s' to become fully available...",
                        self.device_name,
                        self.weather_entity,
                    )
                    await asyncio.sleep(10)
                    continue

            states = [
                state
                for entity_id in self.real_trvs
                if (state := self.hass.states.get(entity_id)) is not None
            ]

            self.bt_min_temp = reduce_attribute(states, ATTR_MIN_TEMP, reduce=max)
            self.bt_max_temp = reduce_attribute(states, ATTR_MAX_TEMP, reduce=min)

            if self.bt_target_temp_step == 0.0:
                self.bt_target_temp_step = reduce_attribute(
                    states, ATTR_TARGET_TEMP_STEP, reduce=max
                )

            self.all_entities.append(self.sensor_entity_id)

            self.cur_temp = convert_to_float(
                str(sensor_state.state), self.device_name, "startup()"
            )
            if self.humidity_entity_id is not None:
                self.all_entities.append(self.humidity_entity_id)
                self._current_humidity = convert_to_float(
                    str(self.hass.states.get(self.humidity_entity_id).state),
                    self.device_name,
                    "startup()",
                )

            if self.cooler_entity_id is not None:
                self.bt_target_cooltemp = convert_to_float(
                    str(
                        self.hass.states.get(self.cooler_entity_id).attributes.get(
                            "temperature"
                        )
                    ),
                    self.device_name,
                    "startup()",
                )

            if self.window_id is not None:
                self.all_entities.append(self.window_id)
                window = self.hass.states.get(self.window_id)

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
                self.window_open = False

            # Check If we have an old state
            old_state = await self.async_get_last_state()
            if old_state is not None:
                # First try to load preset temps from config entry options (preferred durable source)
                entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
                if entry and entry.options.get("bt_preset_temperatures"):
                    _opt_presets = entry.options.get("bt_preset_temperatures")
                    try:
                        if isinstance(_opt_presets, str):
                            _opt_loaded = json.loads(_opt_presets)
                        else:
                            _opt_loaded = _opt_presets
                        if isinstance(_opt_loaded, dict):
                            for k, v in _opt_loaded.items():
                                if k in self._preset_temperatures:
                                    try:
                                        self._preset_temperatures[k] = float(v)
                                    except (TypeError, ValueError):
                                        pass
                            _LOGGER.debug(
                                "better_thermostat %s: Loaded preset temperatures from config entry options.",
                                self.device_name,
                            )
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        _LOGGER.debug(
                            "better_thermostat %s: Failed loading config entry preset temps: %s",
                            self.device_name,
                            exc,
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

                # Restore preset mode if present
                _old_preset = old_state.attributes.get("preset_mode")
                if _old_preset in (
                    [PRESET_NONE] + list(self._preset_temperatures.keys())
                ):
                    self._preset_mode = _old_preset
                else:
                    self._preset_mode = PRESET_NONE

                # Restore stored custom preset temperatures if available
                stored_presets = old_state.attributes.get("bt_preset_temperatures")
                if stored_presets:
                    try:
                        if isinstance(stored_presets, str):
                            loaded = json.loads(stored_presets)
                        elif isinstance(stored_presets, dict):
                            loaded = stored_presets
                        else:
                            loaded = None
                        if isinstance(loaded, dict):
                            for key, value in loaded.items():
                                if (
                                    key in self._preset_temperatures
                                    and value is not None
                                ):
                                    try:
                                        new_val = float(value)
                                        if new_val != self._preset_temperatures[key]:
                                            _LOGGER.debug(
                                                "better_thermostat %s: Restored custom preset %s temperature %s (was %s)",
                                                self.device_name,
                                                key,
                                                new_val,
                                                self._preset_temperatures[key],
                                            )
                                        self._preset_temperatures[key] = new_val
                                    except (ValueError, TypeError):
                                        _LOGGER.warning(
                                            "better_thermostat %s: Could not parse stored preset temperature for %s: %s",
                                            self.device_name,
                                            key,
                                            value,
                                        )
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning(
                            "better_thermostat %s: Failed to restore custom preset temperatures: %s",
                            self.device_name,
                            exc,
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
                    self.heating_power = float(
                        old_state.attributes.get(ATTR_STATE_HEATING_POWER)
                    )
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

            else:
                # No previous state, try and restore defaults
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

            # if hvac mode could not be restored, turn heat off
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

            if self.humidity_entity_id is not None:
                self._current_humidity = convert_to_float(
                    str(self.hass.states.get(self.humidity_entity_id).state),
                    self.device_name,
                    "startup()",
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

            self.async_write_ha_state()

            for trv in self.real_trvs.keys():
                self.all_entities.append(trv)
                await init(self, trv)
                if self.real_trvs[trv]["calibration"] != 1:
                    self.real_trvs[trv]["last_calibration"] = await get_current_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_min"] = await get_min_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_max"] = await get_max_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_step"] = (
                        await get_offset_step(self, trv)
                    )
                else:
                    self.real_trvs[trv]["last_calibration"] = 0

                _s = self.hass.states.get(trv)
                _attrs = _s.attributes if _s else {}
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
                await control_trv(self, trv)

            await self._trigger_time(None)
            await self._trigger_check_weather(None)
            self.startup_running = False
            self._available = True
            self.async_write_ha_state()
            #
            await asyncio.sleep(5)

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

            await check_all_entities(self)

            if self.is_removed:
                return

            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._trigger_check_weather, timedelta(hours=1)
                )
            )

            # Periodischer 5-Minuten-Tick: nur aktivieren, wenn Balance konfiguriert ist
            try:
                any_balance = any(
                    str(
                        (trv_info.get("advanced", {}) or {}).get("balance_mode", "")
                    ).lower()
                    in ("heuristic", "pid")
                    for trv_info in self.real_trvs.values()
                )
            except Exception:  # noqa: BLE001
                any_balance = False

            if any_balance:
                self.async_on_remove(
                    async_track_time_interval(
                        self.hass, self._trigger_time, timedelta(minutes=5)
                    )
                )
                _LOGGER.debug(
                    "better_thermostat %s: 5min balance tick enabled", self.device_name
                )
            else:
                _LOGGER.debug(
                    "better_thermostat %s: 5min balance tick skipped (balance_mode not enabled)",
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
            except Exception:  # noqa: BLE001
                any_maintenance = False

            if any_maintenance:
                self.async_on_remove(
                    async_track_time_interval(
                        self.hass, self._maintenance_tick, timedelta(minutes=5)
                    )
                )
                _LOGGER.debug(
                    "better_thermostat %s: valve maintenance tick enabled (5min)",
                    self.device_name,
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
            if self.humidity_entity_id is not None:
                self.async_on_remove(
                    async_track_state_change_event(
                        self.hass,
                        [self.humidity_entity_id],
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
                self.hass.async_create_task(self._external_temperature_keepalive())
            except Exception:  # noqa: BLE001
                pass
            _LOGGER.info("better_thermostat %s: startup completed.", self.device_name)
            self.async_write_ha_state()
            await self.async_update_ha_state(force_refresh=True)
            break

    async def _maintenance_tick(self, event=None):
        """Periodic maintenance tick: runs valve exercise when due and enabled."""
        # quick availability check
        try:
            ok = await check_all_entities(self)
            if ok is False:
                return
        except Exception:  # noqa: BLE001
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
            self.next_valve_maintenance = now + timedelta(hours=6)
            _LOGGER.debug(
                "better_thermostat %s: valve maintenance postponed (window open)",
                self.device_name,
            )
            return
        if self.hvac_mode == HVACMode.OFF or self.bt_hvac_mode == HVACMode.OFF:
            self.next_valve_maintenance = now + timedelta(hours=6)
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
        except Exception:  # noqa: BLE001
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
        prev_ignore_states = self.ignore_states
        self.ignore_states = True
        now = datetime.now()

        try:
            _LOGGER.info(
                "better_thermostat %s: starting valve maintenance for %d TRV(s)",
                self.device_name,
                len(trvs),
            )

            async def service_one(trv_id: str):
                # Per-TRV guard
                try:
                    self.real_trvs[trv_id]["ignore_trv_states"] = True
                except Exception:
                    pass

                # Read current TRV state safely
                trv_state = self.hass.states.get(trv_id)
                if trv_state is None:
                    _LOGGER.debug(
                        "better_thermostat %s: maintenance skip %s (state None)",
                        self.device_name,
                        trv_id,
                    )
                    return
                cur_mode = trv_state.state
                cur_temp = trv_state.attributes.get("temperature")

                # Capabilities
                valve_entity = (self.real_trvs.get(trv_id, {}) or {}).get(
                    "valve_position_entity"
                )
                quirks = (self.real_trvs.get(trv_id, {}) or {}).get("model_quirks")
                support_valve = bool(valve_entity) or bool(
                    getattr(quirks, "override_set_valve", None)
                )

                # Helper to set valve percent with fallback to quirks
                async def _set_valve_pct(pct: int) -> bool:
                    try:
                        # Prefer unified delegate path; it records method and last percent
                        from .adapters.delegate import set_valve as _delegate_set_valve

                        ok = await _delegate_set_valve(self, trv_id, int(pct))
                        return bool(ok)
                    except Exception:
                        return False

                try:
                    if support_valve:
                        # Open fully
                        _LOGGER.debug(
                            "better_thermostat %s: maintenance %s -> valve 100%%",
                            self.device_name,
                            trv_id,
                        )
                        await _set_valve_pct(100)
                        await asyncio.sleep(20)
                        # Close fully
                        _LOGGER.debug(
                            "better_thermostat %s: maintenance %s -> valve 0%%",
                            self.device_name,
                            trv_id,
                        )
                        await _set_valve_pct(0)
                        await asyncio.sleep(15)
                    else:
                        # Fallback: use temperature extremes to force open/close
                        max_t = (self.real_trvs.get(trv_id, {}) or {}).get(
                            "max_temp", 30
                        )
                        min_t = (self.real_trvs.get(trv_id, {}) or {}).get(
                            "min_temp", 5
                        )
                        # Only run if HVAC is not OFF
                        if cur_mode != HVACMode.OFF:
                            _LOGGER.debug(
                                "better_thermostat %s: maintenance %s -> temp %.1f°C (open)",
                                self.device_name,
                                trv_id,
                                max_t,
                            )
                            await adapter_set_temperature(self, trv_id, max_t)
                            await asyncio.sleep(30)
                            _LOGGER.debug(
                                "better_thermostat %s: maintenance %s -> temp %.1f°C (close)",
                                self.device_name,
                                trv_id,
                                min_t,
                            )
                            await adapter_set_temperature(self, trv_id, min_t)
                            await asyncio.sleep(30)

                    # Restore previous setpoint and mode
                    try:
                        if cur_temp is not None:
                            await adapter_set_temperature(self, trv_id, cur_temp)
                    except Exception:
                        pass
                    try:
                        await adapter_set_hvac_mode(self, trv_id, cur_mode)
                    except Exception:
                        pass
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "better_thermostat %s: maintenance error for %s: %s",
                        self.device_name,
                        trv_id,
                        exc,
                    )
                finally:
                    # Release guard
                    try:
                        self.real_trvs[trv_id]["ignore_trv_states"] = False
                    except Exception:
                        pass

            # Execute sequentially to avoid stressing the system; still lightweight
            for trv in trvs:
                await service_one(trv)

            # Re-arm next run in ~7 days with slight randomization
            self.next_valve_maintenance = now + timedelta(days=7, hours=randint(0, 12))
            _LOGGER.info(
                "better_thermostat %s: valve maintenance finished; next at %s",
                self.device_name,
                self.next_valve_maintenance,
            )
        finally:
            self.ignore_states = prev_ignore_states
            self.in_maintenance = False

    async def _load_balance_state(self) -> None:
        """Load persisted balance states and hydrate module-level cache."""
        if self._balance_store is None:
            return
        data = await self._balance_store.async_load()
        if not data:
            return
        prefix = f"{self._unique_id}:"
        try:
            imported = balance_import_states(data, prefix_filter=prefix)
            _LOGGER.debug(
                "better_thermostat %s: loaded %s balance state(s) with prefix %s",
                self.device_name,
                imported,
                prefix,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug(
                "better_thermostat %s: failed to import balance states: %s",
                self.device_name,
                e,
            )

    async def _save_balance_state(self) -> None:
        """Persist current balance states for this entity (prefix filtered)."""
        if self._balance_store is None:
            return
        try:
            prefix = f"{self._unique_id}:"
            current = balance_export_states(prefix=prefix)
            # Merge with existing store to avoid overwriting other entities' data
            existing = await self._balance_store.async_load()
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
            await self._balance_store.async_save(existing)
            _LOGGER.debug(
                "better_thermostat %s: saved %d balance state(s)",
                self.device_name,
                len(current or {}),
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug(
                "better_thermostat %s: saving balance states failed: %s",
                self.device_name,
                e,
            )

    def _schedule_save_balance_state(self, delay_s: float = 10.0) -> None:
        """Debounced scheduling for saving balance state to storage."""
        if self._balance_store is None or self._balance_save_scheduled:
            return
        self._balance_save_scheduled = True

        async def _delayed_save():
            try:
                await asyncio.sleep(delay_s)
                await self._save_balance_state()
            finally:
                self._balance_save_scheduled = False

        # Fire and forget
        self.hass.async_create_task(_delayed_save())

    async def _load_open_caps(self) -> None:
        """Load persisted open caps map for this entity."""
        if self._open_caps_store is None:
            return
        data = await self._open_caps_store.async_load()
        if not isinstance(data, dict):
            return
        key_prefix = f"{self._unique_id}:"
        # Filter entries for this entity
        entity_map = {}
        for k, v in data.items():
            if not isinstance(k, str) or not k.startswith(key_prefix):
                continue
            try:
                _, trv, bucket = k.split(":", 2)
            except ValueError:
                # legacy/unknown key; skip
                continue
            entity_map.setdefault(trv, {})[bucket] = v
        if entity_map:
            self.open_caps = entity_map

    async def _save_open_caps(self) -> None:
        """Persist learned open caps map for this entity."""
        if self._open_caps_store is None:
            return
        try:
            # Merge into existing store to keep other entities' data
            existing = await self._open_caps_store.async_load()
            if not isinstance(existing, dict):
                existing = {}
            # Remove current entity keys
            key_prefix = f"{self._unique_id}:"
            for k in list(existing.keys()):
                if isinstance(k, str) and k.startswith(key_prefix):
                    del existing[k]
            # Add current entries
            for trv, buckets in (self.open_caps or {}).items():
                for bucket, vals in (buckets or {}).items():
                    existing[f"{self._unique_id}:{trv}:{bucket}"] = vals
            await self._open_caps_store.async_save(existing)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug(
                "better_thermostat %s: saving open caps failed: %s", self.device_name, e
            )

    def _schedule_save_open_caps(self, delay_s: float = 10.0) -> None:
        """Debounced scheduling for saving open caps to storage."""
        if self._open_caps_store is None or self._open_caps_save_scheduled:
            return
        self._open_caps_save_scheduled = True

        async def _delayed_save():
            try:
                await asyncio.sleep(delay_s)
                await self._save_open_caps()
            finally:
                self._open_caps_save_scheduled = False

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
                except Exception:  # noqa: BLE001
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
                self.heating_power = round(
                    old_power * (1 - alpha) + heating_rate * alpha, 4
                )
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
                except Exception:  # noqa: BLE001
                    pass

                _LOGGER.debug(
                    "better_thermostat %s: heating cycle evaluated: ΔT=%.3f°C, t=%.2fmin, rate=%.4f°C/min, hp(old/new)=%.4f/%.4f, alpha=%.3f, env_factor=%.3f, norm=%s",  # noqa: E501
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
            self.async_write_ha_state()

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
            CONF_TOLERANCE: self.tolerance,
            CONF_TARGET_TEMP_STEP: self.bt_target_temp_step,
            ATTR_STATE_HEATING_POWER: self.heating_power,
            ATTR_STATE_ERRORS: json.dumps(self.devices_errors),
            ATTR_STATE_BATTERIES: json.dumps(self.devices_states),
            # Persist current preset temperature mapping so we can restore on restart
            "bt_preset_temperatures": json.dumps(self._preset_temperatures),
            # Flag if user changed at least one preset temperature from original configuration
            "bt_preset_customized": any(
                self._preset_temperatures.get(k) != v
                for k, v in self._original_preset_temperatures.items()
            ),
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

        # (Removed per-user request): per-TRV hvac_action debug attribute

        # Optional telemetry (memory friendly): only count & last cycle + normalized power
        if hasattr(self, "heating_cycles") and len(self.heating_cycles) > 0:
            last_cycle = self.heating_cycles[-1]
            try:
                dev_specific["heating_cycle_count"] = len(self.heating_cycles)
                dev_specific["heating_cycle_last"] = json.dumps(last_cycle)
            except Exception:  # noqa: BLE001
                pass
        if hasattr(self, "heating_power_normalized"):
            dev_specific["heating_power_norm"] = getattr(
                self, "heating_power_normalized", None
            )

        # Optional telemetry (memory friendly): only count & last cycle + normalized power
        if hasattr(self, "heating_cycles") and len(self.heating_cycles) > 0:
            last_cycle = self.heating_cycles[-1]
            try:
                dev_specific["heating_cycle_count"] = len(self.heating_cycles)
                dev_specific["heating_cycle_last"] = json.dumps(last_cycle)
            except Exception:  # noqa: BLE001
                pass
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
                bal = info.get("balance")
                if bal:
                    bal_compact[trv] = {
                        "valve%": bal.get("valve_percent"),
                        "flow_capK": bal.get("flow_cap_K"),
                    }
            if bal_compact:
                dev_specific["balance"] = json.dumps(bal_compact)
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
                bal = (self.real_trvs.get(rep_trv, {}) or {}).get("balance") or {}
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

        # Optional telemetry (memory friendly): only count & last cycle + normalized power
        if hasattr(self, "heating_cycles") and len(self.heating_cycles) > 0:
            last_cycle = self.heating_cycles[-1]
            try:
                dev_specific["heating_cycle_count"] = len(self.heating_cycles)
                dev_specific["heating_cycle_last"] = json.dumps(last_cycle)
            except Exception:  # noqa: BLE001
                pass
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
                bal = info.get("balance")
                if bal:
                    bal_compact[trv] = {
                        "valve%": bal.get("valve_percent"),
                        "flow_capK": bal.get("flow_cap_K"),
                    }
            if bal_compact:
                dev_specific["balance"] = json.dumps(bal_compact)
        except Exception:
            pass

        # Learned open caps: expose ALL buckets per TRV, mark current bucket, and attach suggestions to current
        try:
            caps: dict[str, Any] = {}
            # Bucket rounding to 0.5°C for readability

            def _bucket(temp):
                try:
                    return f"{round(float(temp) * 2.0) / 2.0:.1f}"
                except Exception:
                    return "unknown"

            bucket = _bucket(self.bt_target_temp)
            for trv in self.real_trvs.keys():
                # Collect all learned buckets for this TRV
                trv_learned = (self.open_caps or {}).get(trv, {}) or {}
                buckets_out: dict[str, Any] = {}
                for b, vals in trv_learned.items():
                    buckets_out[b] = {
                        "min_open_pct": vals.get("min_open_pct"),
                        "max_open_pct": vals.get("max_open_pct"),
                    }

                # Attach suggestions for the CURRENT bucket (if available)
                bal = self.real_trvs.get(trv, {}).get("balance") or {}
                if bal:
                    vmin = bal.get("sonoff_min_open_pct")
                    vmax = bal.get("sonoff_max_open_pct")
                    cur_entry = buckets_out.get(bucket, {})
                    if vmin is not None:
                        try:
                            if isinstance(vmin, (int, float)):
                                cur_entry["suggested_min_open_pct"] = int(vmin)
                            elif isinstance(vmin, str):
                                cur_entry["suggested_min_open_pct"] = int(float(vmin))
                        except Exception:  # noqa: BLE001
                            pass
                    if vmax is not None:
                        try:
                            if isinstance(vmax, (int, float)):
                                cur_entry["suggested_max_open_pct"] = int(vmax)
                            elif isinstance(vmax, str):
                                cur_entry["suggested_max_open_pct"] = int(float(vmax))
                        except Exception:  # noqa: BLE001
                            pass
                    if cur_entry:
                        buckets_out[bucket] = cur_entry

                if buckets_out:
                    caps[trv] = {"current_bucket": bucket, "buckets": buckets_out}
            if caps:
                dev_specific["trv_open_caps"] = json.dumps(caps)
            # Flat attributes for current bucket (pick a representative TRV; prefer Sonoff if present)
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
                    learned = (self.open_caps or {}).get(rep_trv, {}).get(bucket) or {}
                    suggested = (self.real_trvs.get(rep_trv, {}) or {}).get(
                        "balance"
                    ) or {}
                    # Extract suggested values safely
                    svalve = suggested.get("valve_percent")
                    smin = suggested.get("sonoff_min_open_pct")
                    smax = suggested.get("sonoff_max_open_pct")
                    svalve_i = None
                    smin_i = None
                    smax_i = None
                    try:
                        if isinstance(svalve, (int, float)):
                            svalve_i = int(svalve)
                        elif isinstance(svalve, str):
                            svalve_i = int(float(svalve))
                    except Exception:
                        pass
                    try:
                        if isinstance(smin, (int, float)):
                            smin_i = int(smin)
                        elif isinstance(smin, str):
                            smin_i = int(float(smin))
                    except Exception:
                        pass
                    try:
                        if isinstance(smax, (int, float)):
                            smax_i = int(smax)
                        elif isinstance(smax, str):
                            smax_i = int(float(smax))
                    except Exception:
                        pass
                    # Root: expose suggested valve percent for convenience
                    if svalve_i is not None:
                        dev_specific["suggested_valve_percent"] = int(
                            max(0, min(100, svalve_i))
                        )
                    # Root: also expose suggested_* for clarity
                    if smin_i is not None:
                        dev_specific["suggested_min_open_pct"] = int(
                            max(0, min(100, smin_i))
                        )
                    if smax_i is not None:
                        dev_specific["suggested_max_open_pct"] = int(
                            max(0, min(100, smax_i))
                        )

                    # Flat attributes should PREFER suggested; fallback to learned
                    lmin = learned.get("min_open_pct")
                    lmax = learned.get("max_open_pct")
                    flat_min = smin_i if smin_i is not None else lmin
                    flat_max = smax_i if smax_i is not None else lmax
                    if flat_min is not None:
                        dev_specific["min_open_pct"] = int(
                            max(0, min(100, int(flat_min)))
                        )
                    if flat_max is not None:
                        dev_specific["max_open_pct"] = int(
                            max(0, min(100, int(flat_max)))
                        )
            except Exception:
                pass
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
    def target_temperature_step(self) -> Optional[float]:
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
    def current_temperature(self) -> Optional[float]:
        """Return the current temperature."""
        return self.cur_temp

    @property
    def current_humidity(self) -> Optional[float]:
        """Return the current humidity if supported."""
        return self._current_humidity if hasattr(self, "_current_humidity") else None

    @property
    def hvac_mode(self) -> Optional[HVACMode]:
        """Return current operation."""
        # Fallback if None
        if self.bt_hvac_mode is None:
            return HVACMode.OFF
        mapped = get_hvac_bt_mode(self, self.bt_hvac_mode)
        if isinstance(mapped, HVACMode):
            return mapped
        try:
            return HVACMode(mapped)
        except Exception:  # noqa: BLE001
            try:
                return HVACMode[mapped.upper()]
            except Exception:  # noqa: BLE001
                return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the list of available operation modes."""
        return self._hvac_list

    @property
    def hvac_action(self):
        """Return the current HVAC action (delegates to helper)."""
        return self._compute_hvac_action()

    def _compute_hvac_action(self):  # helper kept internal for clarity
        """Pure HVAC action computation without side effects.

        Rules:
        - OFF mode returns OFF regardless of temperatures
        - Open window suppresses active heating/cooling (returns IDLE)
        - Heating if cur_temp < target - tolerance (strictly below)
        - Cooling if mode heat_cool and cur_temp > cool_target + tolerance
        - Otherwise IDLE
        """
        if self.bt_target_temp is None or self.cur_temp is None:
            return HVACAction.IDLE
        if self.hvac_mode == HVACMode.OFF or self.bt_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self.window_open:
            return HVACAction.IDLE
        tol = self.tolerance if self.tolerance is not None else 0.0
        # Heating decision
        # Use strict '<' so we do NOT heat when exactly at setpoint (especially when tol=0)
        if self.cur_temp < (self.bt_target_temp - tol):
            return HVACAction.HEATING
        # Cooling decision (if heat_cool mode and cooling setpoint exists)
        if (
            self.hvac_mode in (HVACMode.HEAT_COOL,)
            and self.bt_target_cooltemp is not None
            and self.cur_temp > (self.bt_target_cooltemp + tol)
        ):
            return HVACAction.COOLING
        # Base decision would be IDLE. If any real TRV indicates active heating, override to HEATING.
        try:
            # Skip overrides while we intentionally ignore TRV states or when window is open
            if getattr(self, "ignore_states", False) or getattr(
                self, "window_open", False
            ):
                return HVACAction.IDLE
            # Threshold for valve opening to be considered "heating": 5%

            def _to_pct(val):
                try:
                    v = float(val)
                    return v * 100.0 if v <= 1.0 else v
                except Exception:
                    return None

            THRESH = 5.0
            for trv_id, info in (self.real_trvs or {}).items():
                if not isinstance(info, dict):
                    continue
                if info.get("ignore_trv_states"):
                    continue
                # 0) Nutze zuerst den Cache (events/trv.py pflegt hvac_action), optionaler Fallback auf hass.states
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
                            str(action_raw).lower() if action_raw is not None else ""
                        )
                        # Fallback: wenn wir hier eine Action gefunden haben, sofort in den Cache schreiben,
                        # damit der nächste Durchlauf keine weitere State-Abfrage benötigt.
                        if action_str:
                            try:
                                info["hvac_action"] = action_str
                                # kein Logging hier, um Spam zu vermeiden
                            except Exception:
                                pass
                    if action_str == "heating" or action_val == HVACAction.HEATING:
                        _LOGGER.debug(
                            "better_thermostat %s: overriding hvac_action to HEATING (TRV %s reports heating)",
                            self.device_name,
                            trv_id,
                        )
                        return HVACAction.HEATING
                except Exception:
                    pass
                # 1) Previously we treated hvac_mode=heat as active heating.
                #    This caused false positives for some TRVs that report HEAT while idling.
                #    We now rely on hvac_action/valve signals instead, so skip this shortcut.
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
                        return HVACAction.HEATING
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
                        return HVACAction.HEATING
                except Exception:
                    pass
                # 4) Balance module currently targets a valve percent > 0 (proxy for heating intent)
                bal = info.get("balance") or {}
                v_bal = bal.get("valve_percent") if isinstance(bal, dict) else None
                try:
                    v_bal_n = _to_pct(v_bal)
                    if v_bal_n is not None and v_bal_n > THRESH:
                        _LOGGER.debug(
                            "better_thermostat %s: overriding hvac_action to HEATING (balance.valve_percent %.1f%%, TRV %s)",
                            self.device_name,
                            v_bal_n,
                            trv_id,
                        )
                        return HVACAction.HEATING
                except Exception:
                    pass
        except Exception:
            # Defensive: if anything goes wrong in overrides, fall back to IDLE
            pass

        return HVACAction.IDLE

    @property
    def target_temperature(self) -> Optional[float]:
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
    def target_temperature_low(self) -> Optional[float]:
        if self.cooler_entity_id is None:
            return None
        return self.bt_target_temp

    @property
    def target_temperature_high(self) -> Optional[float]:
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
        await self.control_queue_task.put(self)

    async def async_set_temperature(self, **kwargs) -> None:
        if self.bt_hvac_mode == HVACMode.OFF:
            return
        _LOGGER.debug(
            "better_thermostat %s: async_set_temperature kwargs=%s, current preset=%s, hvac_mode=%s",
            self.device_name,
            kwargs,
            getattr(self, "_preset_mode", None),
            getattr(self, "bt_hvac_mode", None),
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
                "better_thermostat %s: received a new setpoint from HA, but temperature attribute was not set, ignoring",
                self.device_name,
            )
            return

        # Validate against min/max temps
        if _new_setpoint is not None:
            _new_setpoint = min(self.max_temp, max(self.min_temp, _new_setpoint))
        if _new_setpointlow is not None:
            _new_setpointlow = min(self.max_temp, max(self.min_temp, _new_setpointlow))
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
        # update the stored preset temperature so that returning to the preset
        # later reuses the newly chosen value instead of the originally
        # configured one. This applies to ALL presets including PRESET_NONE.
        # Note: We still avoid persisting to config entry options here to
        # prevent frequent integration reloads; persistence can be handled
        # via state restore or an explicit save action.
        if self._preset_mode in self._preset_temperatures and (
            _new_setpoint is not None or _new_setpointlow is not None
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
        await self.control_queue_task.put(self)

    def _async_persist_preset_temperatures(self) -> None:
        """Persist current preset temperature mapping to the config entry options.

        This provides durability even if RestoreState does not keep state (e.g., ephemeral
        test containers). Runs synchronously (HA will write options asynchronously).
        """
        if self.hass is None:
            return
        entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
        if entry is None:
            return
        # Merge existing options keeping unrelated keys
        new_options = dict(entry.options)
        new_options["bt_preset_temperatures"] = self._preset_temperatures
        # Only update if something actually changed to avoid unnecessary writes
        if entry.options.get("bt_preset_temperatures") != self._preset_temperatures:
            self.hass.config_entries.async_update_entry(entry, options=new_options)

        # (Removed misplaced logging/state update; handled in async_set_temperature)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

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
        except Exception:  # noqa: BLE001
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
        elif preset_mode != PRESET_NONE and preset_mode in self._preset_temperatures:
            # Use the configured absolute temperature for this preset
            configured_temp = self._preset_temperatures[preset_mode]

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
            getattr(self, "bt_target_temp", None),
            getattr(self, "bt_hvac_mode", None),
        )

        self.async_write_ha_state()
        if hasattr(self, "control_queue_task") and self.control_queue_task is not None:
            await self.control_queue_task.put(self)

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
        return [
            PRESET_NONE,
            PRESET_AWAY,
            PRESET_BOOST,
            PRESET_SLEEP,
            PRESET_COMFORT,
            PRESET_ECO,
            PRESET_ACTIVITY,
            PRESET_HOME,
        ]

    async def reset_pid_learnings_service(
        self,
        include_open_caps: bool = False,
        apply_pid_defaults: bool = False,
        defaults_kp: Optional[float] = None,
        defaults_ki: Optional[float] = None,
        defaults_kd: Optional[float] = None,
    ) -> None:
        """Entity service: reset learned PID state for this entity.

        - Clears all cached BalanceState entries for this entity (all TRVs/buckets)
        - Optionally clears learned open caps (min/max %) when include_open_caps=True
        - Schedules persistence saves for both maps
        """
        try:
            prefix = f"{self._unique_id}:"
            # Collect keys to reset from balance module
            current = balance_export_states(prefix=prefix) or {}
            count = 0
            for key in list(current.keys()):
                try:
                    balance_reset_state(key)
                    count += 1
                except Exception:
                    pass
            _LOGGER.info(
                "better_thermostat %s: reset %d PID learning state entries (prefix=%s)",
                self.device_name,
                count,
                prefix,
            )
            # Schedule persistence of cleared balance states
            try:
                self._schedule_save_balance_state()
            except Exception:
                pass

            if include_open_caps:
                self.open_caps = {}
                _LOGGER.info(
                    "better_thermostat %s: cleared learned open caps map",
                    self.device_name,
                )
                try:
                    self._schedule_save_open_caps()
                except Exception:
                    pass

            # Optionally seed PID defaults for the CURRENT target bucket(s)
            if apply_pid_defaults:
                try:
                    from .balance import seed_pid_gains, BalanceParams

                    # Use provided overrides or BalanceParams defaults
                    _defs = BalanceParams()
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
                    uid = getattr(self, "unique_id", None) or getattr(
                        self, "_unique_id", "bt"
                    )
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
                            self._schedule_save_balance_state()
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
                            getattr(self, "bt_target_temp", None),
                            buckets,
                        )
                except Exception as e:  # noqa: BLE001
                    _LOGGER.debug(
                        "better_thermostat %s: apply_pid_defaults failed: %s",
                        self.device_name,
                        e,
                    )
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "better_thermostat %s: reset PID learnings service failed: %s",
                self.device_name,
                e,
            )

    async def start_mpc_deadzone_calibration_service(self) -> None:
        """Entity service: start active MPC deadzone calibration.

        This will reset any existing deadzone estimation and immediately start
        an active test sequence (2%, 4%, 6%... valve positions) until the TRV
        responds, definitively measuring the deadzone.
        """
        try:
            from .balance import start_mpc_deadzone_calibration

            prefix = f"{self._unique_id}:"
            count = 0

            # Start calibration for all TRVs in this entity
            for trv_id in self.real_trvs.keys():
                # MPC uses the same bucket keys as PID
                # Try common bucket patterns (e.g., 20.0, 21.0, etc.)
                # In practice, we may need to trigger calibration for the current active bucket
                try:
                    # Get current target temp to build bucket tag
                    # Note: bucket_tag format must match trv.py: "t{temp:.1f}" with 0.5°C rounding
                    current_target = getattr(self, "bt_target_temp", None)
                    if current_target is not None:
                        # Round to 0.5°C steps like in trv.py
                        bucket_temp = round(float(current_target) * 2.0) / 2.0
                        bucket_tag = f"t{bucket_temp:.1f}"
                        key = f"{prefix}{trv_id}:{bucket_tag}"
                        if start_mpc_deadzone_calibration(key):
                            count += 1
                            _LOGGER.info(
                                "better_thermostat %s: started MPC deadzone calibration for TRV %s (key=%s)",
                                self.device_name,
                                trv_id,
                                key,
                            )
                except Exception as e:  # noqa: BLE001
                    _LOGGER.debug(
                        "better_thermostat %s: failed to start calibration for TRV %s: %s",
                        self.device_name,
                        trv_id,
                        e,
                    )

            if count > 0:
                _LOGGER.info(
                    "better_thermostat %s: started MPC deadzone calibration for %d TRV(s)",
                    self.device_name,
                    count,
                )
                # Trigger control loop to start calibration immediately
                try:
                    await self.control_queue_task.put(self)
                except Exception:
                    pass
            else:
                _LOGGER.warning(
                    "better_thermostat %s: no MPC states found to calibrate (prefix=%s, target_temp=%s)",
                    self.device_name,
                    prefix,
                    getattr(self, "bt_target_temp", None),
                )
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "better_thermostat %s: start_mpc_deadzone_calibration_service failed: %s",
                self.device_name,
                e,
            )
