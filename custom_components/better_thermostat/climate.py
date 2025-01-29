"""Better Thermostat"""

import asyncio
import json
import logging
from abc import ABC
from datetime import datetime, timedelta
from random import randint
from statistics import mean

# Home Assistant imports
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    PRESET_NONE,
)
from homeassistant.components.climate.const import (
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
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

# Local imports
from .adapters.delegate import (
    get_current_offset,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    init,
    load_adapter,
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
    ATTR_STATE_WINDOW_OPEN,
    ATTR_STATE_DOOR_OPEN,  # Neues Attribut für Türstatus
    BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
    CONF_COOLER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_SENSOR_DOOR,  # Hinzugefügt
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    CONF_DOOR_TIMEOUT,  # Hinzugefügt
    CONF_DOOR_TIMEOUT_AFTER,  # Hinzugefügt
    SUPPORT_FLAGS,
    VERSION,
    SERVICE_SET_TEMP_TARGET_TEMPERATURE,  # Hinzugefügt
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE,  # Hinzugefügt
    SERVICE_RESET_HEATING_POWER,  # Hinzugefügt
)
from .utils.controlling import control_queue, control_trv
from .utils.helpers import convert_to_float, find_battery_entity, get_hvac_bt_mode
from .utils.watcher import check_all_entities
from .utils.weather import check_ambient_air_temperature, check_weather


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


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Better Thermostat platform."""
    platform = entity_platform.async_get_current_platform()

    # Register service validators
    platform.async_register_service_validator(
        "set_temperature", async_set_temperature_service_validate
    )


async def async_setup_entry(hass, entry, async_add_devices):
    """Setup sensor platform."""

    async def async_service_handler(entity, call):
        """Handle the service calls."""
        if call.service == SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE:
            await entity.restore_temp_temperature()
        elif call.service == SERVICE_SET_TEMP_TARGET_TEMPERATURE:
            await entity.set_temp_temperature(call.data[ATTR_TEMPERATURE])
        elif call.service == SERVICE_RESET_HEATING_POWER:
            await entity.reset_heating_power()

    platform = entity_platform.async_get_current_platform()
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

    async_add_devices(
        [
            BetterThermostat(
                entry.data.get(CONF_NAME),
                entry.data.get(CONF_HEATER),
                entry.data.get(CONF_SENSOR),
                entry.data.get(CONF_HUMIDITY, None),
                entry.data.get(CONF_SENSOR_WINDOW, None),
                entry.data.get(CONF_WINDOW_TIMEOUT, None),
                entry.data.get(CONF_WINDOW_TIMEOUT_AFTER, None),
                entry.data.get(CONF_SENSOR_DOOR, None),  # Hinzugefügt
                entry.data.get(CONF_DOOR_TIMEOUT, None),  # Hinzugefügt
                entry.data.get(CONF_DOOR_TIMEOUT_AFTER, None),  # Hinzugefügt
                entry.data.get(CONF_WEATHER, None),
                entry.data.get(CONF_OUTDOOR_SENSOR, None),
                entry.data.get(CONF_OFF_TEMPERATURE, 0),  # Standardwert 0 wenn None
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
        door_id,  # Hinzugefügt
        door_delay,  # Hinzugefügt
        door_delay_after,  # Hinzugefügt
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
        self.door_id = door_id or None  # Hinzugefügt
        self.door_delay = door_delay or 0  # Hinzugefügt
        self.door_delay_after = door_delay_after or 0  # Hinzugefügt
        self.weather_entity = weather_entity or None
        self.outdoor_sensor = outdoor_sensor or None
        self.off_temperature = float(off_temperature)  # Standardwert ist bereits 0
        self.tolerance = float(tolerance) or 0.0
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
        self.door_open = None  # Hinzugefügt
        self.bt_target_temp_step = float(target_temp_step) or 0.0
        self.bt_min_temp = 0
        self.bt_max_temp = 30
        self.bt_target_temp = 5.0
        self.bt_target_cooltemp = None
        self._support_flags = SUPPORT_FLAGS | ClimateEntityFeature.PRESET_MODE
        self.bt_hvac_mode = None
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
        self.last_avg_outdoor_temp = None
        self.last_main_hvac_mode = None
        self.last_window_state = None
        self.last_door_state = None  # Hinzugefügt
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
        self.last_heating_power_stats = []
        self.is_removed = False

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
            if trv["advanced"]["calibration"] == "local_calibration_based":
                _calibration = 0
            if trv["advanced"]["calibration"] == "hybrid_calibration":
                _calibration = 2
            _adapter = await load_adapter(self, trv["integration"], trv["trv"])
            _model_quirks = await load_model_quirks(self, trv["model"], trv["trv"])
            self.real_trvs[trv["trv"]] = {
                "calibration": _calibration,
                "integration": trv["integration"],
                "adapter": _adapter,
                "model_quirks": _model_quirks,
                "model": trv["model"],
                "advanced": trv["advanced"],
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

    async def _trigger_door_change(self, event):  # Hinzugefügt
        _check = await check_all_entities(self)
        if (_check is False):
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        door_state = self.hass.states.get(self.door_id)
        if door_state.state in ("on", "open", "true"):
            self.door_open = True
        else:
            self.door_open = False
