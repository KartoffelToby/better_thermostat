"""Better Thermostat"""

import asyncio
import logging
from abc import ABC
from datetime import datetime, timedelta
from random import randint
from statistics import mean

from .utils.watcher import check_all_entities

from .utils.weather import check_ambient_air_temperature, check_weather
from .utils.bridge import (
    get_current_offset,
    get_min_offset,
    get_max_offset,
    init,
    load_adapter,
)

from .utils.model_quirks import load_model_quirks

from .utils.helpers import convert_to_float, find_battery_entity
from homeassistant.helpers import entity_platform
from homeassistant.core import callback, CoreState, Context, ServiceCall
import json
from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_STEP,
    HVACMode,
    HVACAction,
)
from homeassistant.const import (
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from homeassistant.components.group.util import reduce_attribute

from .const import (
    ATTR_STATE_BATTERIES,
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_ERRORS,
    ATTR_STATE_HUMIDIY,
    ATTR_STATE_LAST_CHANGE,
    ATTR_STATE_MAIN_MODE,
    ATTR_STATE_WINDOW_OPEN,
    ATTR_STATE_SAVED_TEMPERATURE,
    ATTR_STATE_HEATING_POWER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE,
    SUPPORT_FLAGS,
    VERSION,
    SERVICE_SET_TEMP_TARGET_TEMPERATURE,
    SERVICE_RESET_HEATING_POWER,
    BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
    BetterThermostatEntityFeature,
)

from .utils.controlling import control_queue, control_trv
from .events.temperature import trigger_temperature_change
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"


class ContinueLoop(Exception):
    pass


async def async_setup_entry(hass, entry, async_add_devices):
    """Setup sensor platform."""

    async def async_service_handler(self, data: ServiceCall):
        _LOGGER.debug(f"Service call: {self} » {data.service}")
        if data.service == SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE:
            await self.restore_temp_temperature()
        elif data.service == SERVICE_SET_TEMP_TARGET_TEMPERATURE:
            await self.set_temp_temperature(data.data[ATTR_TEMPERATURE])
        elif data.service == SERVICE_RESET_HEATING_POWER:
            await self.reset_heating_power

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_TEMP_TARGET_TEMPERATURE,
        BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,  # type: ignore
        async_service_handler,
        [
            BetterThermostatEntityFeature.TARGET_TEMPERATURE,
            BetterThermostatEntityFeature.TARGET_TEMPERATURE_RANGE,
        ],
    )
    platform.async_register_entity_service(
        SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE, {}, async_service_handler
    )
    platform.async_register_entity_service(
        SERVICE_RESET_HEATING_POWER, {}, async_service_handler
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
                entry.data.get(CONF_WEATHER, None),
                entry.data.get(CONF_OUTDOOR_SENSOR, None),
                entry.data.get(CONF_OFF_TEMPERATURE, None),
                entry.data.get(CONF_MODEL, None),
                hass.config.units.temperature_unit,
                entry.entry_id,
                device_class="better_thermostat",
                state_class="better_thermostat_state",
            )
        ]
    )


class BetterThermostat(ClimateEntity, RestoreEntity, ABC):
    """Representation of a Better Thermostat device."""

    async def set_temp_temperature(self, temperature):
        if self._saved_temperature is None:
            self._saved_temperature = self.bt_target_temp
            self.bt_target_temp = convert_to_float(
                temperature, self.name, "service.settarget_temperature()"
            )
            self.async_write_ha_state()
            await self.control_queue_task.put(self)
        else:
            self.bt_target_temp = convert_to_float(
                temperature, self.name, "service.settarget_temperature()"
            )
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def savetarget_temperature(self):
        self._saved_temperature = self.bt_target_temp
        self.async_write_ha_state()

    async def restore_temp_temperature(self):
        if self._saved_temperature is not None:
            self.bt_target_temp = convert_to_float(
                self._saved_temperature, self.name, "service.restore_temp_temperature()"
            )
            self._saved_temperature = None
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def reset_heating_power(self):
        self.heating_power = 0.01
        self.async_write_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
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
        weather_entity,
        outdoor_sensor,
        off_temperature,
        model,
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
        self._name = name
        self.model = model
        self.real_trvs = {}
        self.entity_ids = []
        self.all_trvs = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.humidity_entity_id = humidity_sensor_entity_id
        self.window_id = window_id or None
        self.window_delay = window_delay or 0
        self.weather_entity = weather_entity or None
        self.outdoor_sensor = outdoor_sensor or None
        self.off_temperature = float(off_temperature) or None
        self._unique_id = unique_id
        self._unit = unit
        self._device_class = device_class
        self._state_class = state_class
        self._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
        self.next_valve_maintenance = datetime.now() + timedelta(
            hours=randint(1, 24 * 5)
        )
        self.cur_temp = None
        self.cur_humidity = 0
        self.window_open = None
        self.bt_target_temp_step = 1
        self.bt_min_temp = 0
        self.bt_max_temp = 30
        self.bt_target_temp = 5
        self._support_flags = SUPPORT_FLAGS
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
        self.old_external_temp = 0
        self.old_internal_temp = 0
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

        self.entity_ids = [
            entity for trv in self.all_trvs if (entity := trv["trv"]) is not None
        ]

        for trv in self.all_trvs:
            _calibration = 1
            if trv["advanced"]["calibration"] == "local_calibration_based":
                _calibration = 0
            _adapter = load_adapter(self, trv["integration"], trv["trv"])
            _model_quirks = load_model_quirks(self, trv["model"], trv["trv"])
            self.real_trvs[trv["trv"]] = {
                "calibration": _calibration,
                "integration": trv["integration"],
                "adapter": _adapter,
                "model_quirks": _model_quirks,
                "model": trv["model"],
                "advanced": trv["advanced"],
                "ignore_trv_states": False,
                "valve_position": None,
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

        await super().async_added_to_hass()

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.name
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
        check_weather(self)
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
        _LOGGER.debug("better_thermostat %s: get last avg outdoor temps...", self.name)
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
        self.cur_humidity = convert_to_float(
            str(self.hass.states.get(self.humidity_entity_id).state),
            self.name,
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

    async def startup(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        while self.startup_running:
            _LOGGER.info(
                "better_thermostat %s: Starting version %s. Waiting for entity to be ready...",
                self.name,
                self.version,
            )
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if sensor_state is not None:
                if sensor_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
                        self.name,
                        self.sensor_entity_id,
                    )
                    await asyncio.sleep(10)
                    continue

            try:
                for trv in self.real_trvs.keys():
                    trv_state = self.hass.states.get(trv)
                    if trv_state is None:
                        _LOGGER.info(
                            "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                            self.name,
                            trv,
                        )
                        await asyncio.sleep(10)
                        raise ContinueLoop
                    if trv_state is not None:
                        if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                            _LOGGER.info(
                                "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                                self.name,
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
                        self.name,
                        self.window_id,
                    )
                    await asyncio.sleep(10)
                    continue

            if self.humidity_entity_id is not None:
                if self.hass.states.get(self.humidity_entity_id).state in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                    None,
                ):
                    _LOGGER.info(
                        "better_thermostat %s: waiting for humidity sensor entity with id '%s' to become fully available...",
                        self.name,
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
                        self.name,
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
                        self.name,
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
            self.bt_target_temp_step = reduce_attribute(
                states, ATTR_TARGET_TEMP_STEP, reduce=max
            )

            self.all_entities.append(self.sensor_entity_id)

            self.cur_temp = convert_to_float(
                str(sensor_state.state), self.name, "startup()"
            )
            if self.humidity_entity_id is not None:
                self.all_entities.append(self.humidity_entity_id)
                self.cur_humidity = convert_to_float(
                    str(self.hass.states.get(self.humidity_entity_id).state),
                    self.name,
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
                    self.name,
                    "Open" if self.window_open else "Closed",
                )
            else:
                self.window_open = False

            # Check If we have an old state
            old_state = await self.async_get_last_state()
            if old_state is not None:
                # If we have no initial temperature, restore
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self.bt_target_temp = reduce_attribute(
                        states, ATTR_TEMPERATURE, reduce=lambda *data: mean(data)
                    )
                    _LOGGER.debug(
                        "better_thermostat %s: Undefined target temperature, falling back to %s",
                        self.name,
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
                            self.name,
                            _oldtarget_temperature,
                            self.bt_min_temp,
                        )
                        _oldtarget_temperature = self.bt_min_temp
                    # if the saved temperature is higher than the max_temp, set it to max_temp
                    elif _oldtarget_temperature > self.bt_max_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is higher than max_temp %s, setting to max_temp",
                            self.name,
                            _oldtarget_temperature,
                            self.bt_min_temp,
                        )
                        _oldtarget_temperature = self.bt_max_temp
                    self.bt_target_temp = convert_to_float(
                        str(_oldtarget_temperature), self.name, "startup()"
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
                        self.name,
                        "startup()",
                    )
                if old_state.attributes.get(ATTR_STATE_HUMIDIY, None) is not None:
                    self.cur_humidity = old_state.attributes.get(ATTR_STATE_HUMIDIY)
                if old_state.attributes.get(ATTR_STATE_MAIN_MODE, None) is not None:
                    self.last_main_hvac_mode = old_state.attributes.get(
                        ATTR_STATE_MAIN_MODE
                    )
                if old_state.attributes.get(ATTR_STATE_HEATING_POWER, None) is not None:
                    self.heating_power = float(
                        old_state.attributes.get(ATTR_STATE_HEATING_POWER)
                    )

            else:
                # No previous state, try and restore defaults
                if self.bt_target_temp is None or type(self.bt_target_temp) != float:
                    _LOGGER.info(
                        "better_thermostat %s: No previously saved temperature found on startup, get it from the TRV",
                        self.name,
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
                        self.name,
                        self.bt_hvac_mode,
                    )
                # return off if all are off
                elif all(x.state == HVACMode.OFF for x in states):
                    self.bt_hvac_mode = HVACMode.OFF
                    _LOGGER.debug(
                        "better_thermostat %s: No previously hvac mode found on startup, turn bt to trv mode %s",
                        self.name,
                        self.bt_hvac_mode,
                    )
                else:
                    _LOGGER.warning(
                        "better_thermostat %s: No previously hvac mode found on startup, turn heat off",
                        self.name,
                    )
                    self.bt_hvac_mode = HVACMode.OFF

            _LOGGER.debug(
                "better_thermostat %s: Startup config, BT hvac mode is %s, Target temp %s",
                self.name,
                self.bt_hvac_mode,
                self.bt_target_temp,
            )

            if self.last_main_hvac_mode is None:
                self.last_main_hvac_mode = self.bt_hvac_mode

            if self.humidity_entity_id is not None:
                self.cur_humidity = convert_to_float(
                    str(self.hass.states.get(self.humidity_entity_id).state),
                    self.name,
                    "startup()",
                )
            else:
                self.cur_humidity = 0

            self.last_window_state = self.window_open
            if self.bt_hvac_mode not in (HVACMode.OFF, HVACMode.HEAT):
                self.bt_hvac_mode = HVACMode.HEAT

            self.async_write_ha_state()

            for trv in self.real_trvs.keys():
                self.all_entities.append(trv)
                await init(self, trv)
                if self.real_trvs[trv]["calibration"] == 0:
                    self.real_trvs[trv]["last_calibration"] = await get_current_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_min"] = await get_min_offset(
                        self, trv
                    )
                    self.real_trvs[trv]["local_calibration_max"] = await get_max_offset(
                        self, trv
                    )
                else:
                    self.real_trvs[trv]["last_calibration"] = 0

                self.real_trvs[trv]["valve_position"] = convert_to_float(
                    str(
                        self.hass.states.get(trv).attributes.get("valve_position", None)
                    ),
                    self.name,
                    "startup",
                )
                self.real_trvs[trv]["max_temp"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("max_temp", 30)),
                    self.name,
                    "startup",
                )
                self.real_trvs[trv]["min_temp"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("min_temp", 5)),
                    self.name,
                    "startup",
                )
                self.real_trvs[trv]["target_temp_step"] = convert_to_float(
                    str(
                        self.hass.states.get(trv).attributes.get("target_temp_step", 1)
                    ),
                    self.name,
                    "startup",
                )
                self.real_trvs[trv]["temperature"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("temperature", 5)),
                    self.name,
                    "startup",
                )
                self.real_trvs[trv]["hvac_modes"] = self.hass.states.get(
                    trv
                ).attributes.get("hvac_modes", None)
                self.real_trvs[trv]["hvac_mode"] = self.hass.states.get(trv).state
                self.real_trvs[trv]["last_hvac_mode"] = self.hass.states.get(trv).state
                self.real_trvs[trv]["last_temperature"] = convert_to_float(
                    str(self.hass.states.get(trv).attributes.get("temperature")),
                    self.name,
                    "startup()",
                )
                self.real_trvs[trv]["current_temperature"] = convert_to_float(
                    str(
                        self.hass.states.get(trv).attributes.get("current_temperature")
                        or 5
                    ),
                    self.name,
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

            # update_hvac_action(self)
            # Add listener
            if self.outdoor_sensor is not None:
                self.all_entities.append(self.outdoor_sensor)
                self.async_on_remove(
                    async_track_time_change(self.hass, self._trigger_time, 5, 0, 0)
                )

            await check_all_entities(self)

            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._trigger_check_weather, timedelta(hours=1)
                )
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
            _LOGGER.info("better_thermostat %s: startup completed.", self.name)
            self.async_write_ha_state()
            await self.async_update_ha_state(force_refresh=True)
            break

    async def calculate_heating_power(self):
        if (
            self.heating_start_temp is not None
            and self.heating_end_temp is not None
            and self.cur_temp < self.heating_end_temp
        ):
            _temp_diff = self.heating_end_temp - self.heating_start_temp
            _time_diff_minutes = round(
                (self.heating_end_timestamp - self.heating_start_timestamp).seconds
                / 60.0,
                1,
            )
            if _time_diff_minutes > 1:
                _degrees_time = round(_temp_diff / _time_diff_minutes, 4)
                self.heating_power = round(
                    (self.heating_power * 0.9 + _degrees_time * 0.1), 4
                )
                if len(self.last_heating_power_stats) >= 10:
                    self.last_heating_power_stats = self.last_heating_power_stats[
                        len(self.last_heating_power_stats) - 9 :
                    ]
                self.last_heating_power_stats.append(
                    {
                        "temp_diff": round(_temp_diff, 1),
                        "time": _time_diff_minutes,
                        "degrees_time": round(_degrees_time, 4),
                        "heating_power": round(self.heating_power, 4),
                    }
                )
                _LOGGER.debug(
                    f"better_thermostat {self.name}: calculate_heating_power / temp_diff: {round(_temp_diff, 1)} - time: {_time_diff_minutes} - degrees_time: {round(_degrees_time, 4)} - heating_power: {round(self.heating_power, 4)} - heating_power_stats: {self.last_heating_power_stats}"
                )
            # reset for the next heating start
            self.heating_start_temp = None
            self.heating_end_temp = None

        # heating starts
        if (
            self.attr_hvac_action == HVACAction.HEATING
            and self.old_attr_hvac_action != HVACAction.HEATING
        ):
            self.heating_start_temp = self.cur_temp
            self.heating_start_timestamp = datetime.now()
        # heating stops
        elif (
            self.attr_hvac_action != HVACAction.HEATING
            and self.old_attr_hvac_action == HVACAction.HEATING
            and self.heating_start_temp is not None
            and self.heating_end_temp is None
        ):
            self.heating_end_temp = self.cur_temp
            self.heating_end_timestamp = datetime.now()
        # check if the temp is still rising, after heating stopped
        elif (
            self.attr_hvac_action != HVACAction.HEATING
            and self.heating_start_temp is not None
            and self.heating_end_temp is not None
            and self.cur_temp > self.heating_end_temp
        ):
            self.heating_end_temp = self.cur_temp

        if self.attr_hvac_action != self.old_attr_hvac_action:
            self.old_attr_hvac_action = self.attr_hvac_action
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        """Return the device specific state attributes.

        Returns
        -------
        dict
                Attribute dictionary for the extra device specific state attributes.
        """
        dev_specific = {
            ATTR_STATE_WINDOW_OPEN: self.window_open,
            ATTR_STATE_CALL_FOR_HEAT: self.call_for_heat,
            ATTR_STATE_LAST_CHANGE: self.last_change,
            ATTR_STATE_SAVED_TEMPERATURE: self._saved_temperature,
            ATTR_STATE_HUMIDIY: self.cur_humidity,
            ATTR_STATE_MAIN_MODE: self.last_main_hvac_mode,
            ATTR_STATE_HEATING_POWER: self.heating_power,
            ATTR_STATE_ERRORS: json.dumps(self.devices_errors),
            ATTR_STATE_BATTERIES: json.dumps(self.devices_states),
        }

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
    def name(self):
        """Return the name of the thermostat.

        Returns
        -------
        string
                The name of the thermostat.
        """
        return self._name

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
    def target_temperature_step(self):
        """Return the supported step of target temperature.

        Returns
        -------
        float
                Steps of target temperature.
        """
        if self.bt_target_temp_step is not None:
            return self.bt_target_temp_step

        return super().precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement.

        Returns
        -------
        string
                The unit of measurement.
        """
        return self._unit

    @property
    def current_temperature(self):
        """Return the sensor temperature.

        Returns
        -------
        float
                The measured temperature.
        """
        return self.cur_temp

    @property
    def hvac_mode(self):
        """Return current operation.

        Returns
        -------
        string
                HVAC mode only from homeassistant.components.climate.const is valid
        """
        return self.bt_hvac_mode

    @property
    def hvac_action(self):
        """Return the current HVAC action"""
        if (
            self.attr_hvac_action is None
            and self.bt_target_temp is not None
            and self.cur_temp is not None
        ):
            if self.bt_target_temp > self.cur_temp and self.window_open is False:
                self.attr_hvac_action = HVACAction.HEATING
            else:
                self.attr_hvac_action = HVACAction.IDLE
        return self.attr_hvac_action

    @property
    def target_temperature(self):
        """Return the temperature we try to reach.

        Returns
        -------
        float
                Target temperature.
        """
        if None in (self.bt_max_temp, self.bt_min_temp, self.bt_target_temp):
            return self.bt_target_temp
        # if target temp is below minimum, return minimum
        if self.bt_target_temp < self.bt_min_temp:
            return self.bt_min_temp
        # if target temp is above maximum, return maximum
        if self.bt_target_temp > self.bt_max_temp:
            return self.bt_max_temp
        return self.bt_target_temp

    @property
    def hvac_modes(self):
        """List of available operation modes.

        Returns
        -------
        array
                A list of HVAC modes only from homeassistant.components.climate.const is valid
        """
        return self._hvac_list

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        """Set hvac mode.

        Returns
        -------
        None
        """
        if hvac_mode in (HVACMode.HEAT, HVACMode.OFF):
            self.bt_hvac_mode = hvac_mode
        else:
            _LOGGER.error(
                "better_thermostat %s: Unsupported hvac_mode %s", self.name, hvac_mode
            )
        self.async_write_ha_state()
        await self.control_queue_task.put(self)

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature.

        Parameters
        ----------
        kwargs :
                Arguments piped from HA.

        Returns
        -------
        None
        """
        _new_setpoint = convert_to_float(
            str(kwargs.get(ATTR_TEMPERATURE, None)),
            self.name,
            "controlling.settarget_temperature()",
        )
        if _new_setpoint is None:
            _LOGGER.debug(
                f"better_thermostat {self.name}: received a new setpoint from HA, but temperature attribute was not set, ignoring"
            )
            return
        self.bt_target_temp = _new_setpoint
        self.async_write_ha_state()
        await self.control_queue_task.put(self)

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
        return self._support_flags
