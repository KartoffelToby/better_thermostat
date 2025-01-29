"""Better Thermostat Climate Platform."""

import asyncio
import logging
from datetime import datetime, timedelta
from random import randint

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
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, CoreState, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
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
from .utils.helpers import convert_to_float

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"

class BetterThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Better Thermostat device."""

    _attr_has_entity_name = True
    _attr_name = None

    async def set_temp_temperature(self, temperature):
        """Set temporary target temperature."""
        if self._saved_temperature is None:
            self._saved_temperature = self.bt_target_temp
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
    ):
        """Initialize the thermostat."""
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
        if self.door_id is not None:  # Hinzugefügt
            self.door_queue_task = asyncio.Queue(maxsize=1)  # Hinzugefügt
        asyncio.create_task(self.control_queue())
        if self.window_id is not None:
            asyncio.create_task(self.window_queue())
        if self.door_id is not None:  # Hinzugefügt
            asyncio.create_task(self.door_queue())  # Hinzugefügt

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
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

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self.entity_ids, self._trigger_trv_change
            )
        )

        if self.window_id is not None:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.window_id], self._trigger_window_change
                )
            )

        if self.door_id is not None:  # Hinzugefügt
            self.async_on_remove(  # Hinzugefügt
                async_track_state_change_event(  # Hinzugefügt
                    self.hass, [self.door_id], self._trigger_door_change  # Hinzugefügt
                )  # Hinzugefügt
            )  # Hinzugefügt

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.device_name
        )

        @callback
        def _async_startup(*_):
            """Init on startup."""
            self.context = Context()
            loop = asyncio.get_event_loop()
            loop.create_task(self.startup())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def _trigger_check_weather(self, event=None):
        _check = await self.check_all_entities()
        if _check is False:
            return
        await self.check_weather()
        if self._last_call_for_heat != self.call_for_heat:
            self._last_call_for_heat = self.call_for_heat
            await self.async_update_ha_state(force_refresh=True)
            self.async_write_ha_state()
            if event is not None:
                await self.control_queue_task.put(self)

    async def _trigger_time(self, event=None):
        _check = await self.check_all_entities()
        if _check is False:
            return
        _LOGGER.debug(
            "better_thermostat %s: get last avg outdoor temps...", self.device_name
        )
        await self.check_ambient_air_temperature()
        self.async_write_ha_state()
        if event is not None:
            await self.control_queue_task.put(self)

    async def _trigger_temperature_change(self, event):
        _check = await self.check_all_entities()
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return
        self.hass.async_create_task(self.trigger_temperature_change(event))

    async def _trigger_humidity_change(self, event):
        _check = await self.check_all_entities()
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
        _check = await self.check_all_entities()
        if _check is False:
            return
        self.async_set_context(event.context)
        if self._async_unsub_state_changed is None:
            return

        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(self.trigger_trv_change(event))

    async def _trigger_window_change(self, event):
        _check = await self.check_all_entities()
        if _check is False:
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(self.trigger_window_change(event))

    async def _trigger_door_change(self, event):  # Hinzugefügt
        _check = await self.check_all_entities()
        if (_check is False):
            return
        self.async_set_context(event.context)
        if (event.data.get("new_state")) is None:
            return

        self.hass.async_create_task(self.trigger_door_change(event))  # Hinzugefügt

    def hvac_action(self):
        """Return the current HVAC action"""
        if self.bt_target_temp is not None and self.cur_temp is not None:
            if self.hvac_mode == HVACMode.OFF:
                self.attr_hvac_action = HVACAction.OFF
            elif (
                self.bt_target_temp > self.cur_temp + self.tolerance
                and self.window_open is False
                and self.door_open is False  # Türsensoren berücksichtigt
            ):
                self.attr_hvac_action = HVACAction.HEATING
            elif (
                self.bt_target_temp > self.cur_temp
                and self.window_open is False
                and self.door_open is False  # Türsensoren berücksichtigt
                and self.bt_hvac_mode is not HVACMode.OFF
            ):
                self.attr_hvac_action = HVACAction.HEATING
            else:
                self.attr_hvac_action = HVACAction.IDLE
        return self.attr_hvac_action

    async def control_queue(self):
        """Control queue task."""
        while True:
            try:
                await asyncio.sleep(1)
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

                if self.door_id is not None:  # Hinzugefügt
                    if self.hass.states.get(self.door_id).state in (  # Hinzugefügt
                        STATE_UNAVAILABLE,  # Hinzugefügt
                        STATE_UNKNOWN,  # Hinzugefügt
                        None,  # Hinzugefügt
                    ):  # Hinzugefügt
                        _LOGGER.info(  # Hinzugefügt
                            "better_thermostat %s: waiting for door sensor entity with id '%s' to become fully available...",  # Hinzugefügt
                            self.device_name,  # Hinzugefügt
                            self.door_id,  # Hinzugefügt
                        )  # Hinzugefügt
                        await asyncio.sleep(10)  # Hinzugefügt
                        continue  # Hinzugefügt

                await self.check_all_entities()
            except asyncio.CancelledError:
                break

    async def window_queue(self):
        """Window queue task."""
        while True:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

    async def door_queue(self):  # Hinzugefügt
        """Door queue task."""  # Hinzugefügt
        while True:  # Hinzugefügt
            try:  # Hinzugefügt
                await asyncio.sleep(1)  # Hinzugefügt
            except asyncio.CancelledError:  # Hinzugefügt
                break  # Hinzugefügt

    async def startup(self):
        """Startup tasks."""
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

        if self.door_id is not None:  # Hinzugefügt
            self.all_entities.append(self.door_id)  # Hinzugefügt
            door = self.hass.states.get(self.door_id)  # Hinzugefügt

            check = door.state  # Hinzugefügt
            if check in ("on", "open", "true"):  # Hinzugefügt
                self.door_open = True  # Hinzugefügt
            else:  # Hinzugefügt
                self.door_open = False  # Hinzugefügt
            _LOGGER.debug(  # Hinzugefügt
                "better_thermostat %s: detected door state at startup: %s",  # Hinzugefügt
                self.device_name,  # Hinzugefügt
                "Open" if self.door_open else "Closed",  # Hinzugefügt
            )  # Hinzugefügt
        else:  # Hinzugefügt
            self.door_open = False  # Hinzugefügt

        self.last_window_state = self.window_open
        self.last_door_state = self.door_open  # Hinzugefügt
        if self.bt_hvac_mode not in (
            HVACMode.OFF,
            HVACMode.HEAT_COOL,
            HVACMode.HEAT,
        ):
            self.bt_hvac_mode = HVACMode.HEAT

    async def check_all_entities(self):
        """Check all entities."""
        if self.bt_hvac_mode == HVACMode.OFF:
            return False
        if self.window_open:
            return False
        if self.door_open:  # Hinzugefügt
            return False  # Hinzugefügt
        return True

    async def trigger_temperature_change(self, event):
        """Handle temperature change."""
        pass

    async def trigger_trv_change(self, event):
        """Handle TRV change."""
        pass

    async def trigger_window_change(self, event):
        """Handle window change."""
        pass

    async def trigger_door_change(self, event):  # Hinzugefügt
        """Handle door change."""  # Hinzugefügt
        pass
