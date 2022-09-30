"""Better Thermostat"""

import asyncio
import logging
from abc import ABC
from datetime import datetime, timedelta
from random import randint

from custom_components.better_thermostat.weather import check_ambient_air_temperature
from .helpers import convert_to_float, find_local_calibration_entity, mode_remap
from homeassistant.helpers import entity_platform

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
)
from homeassistant.const import (
    CONF_NAME,
    EVENT_HOMEASSISTANT_START,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import callback, CoreState
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN
from .const import (
    ATTR_STATE_CALL_FOR_HEAT,
    ATTR_STATE_LAST_CHANGE,
    ATTR_STATE_WINDOW_OPEN,
    ATTR_STATE_DAY_SET_TEMP,
    ATTR_STATE_SAVED_TEMPERATURE,
    CONF_CALIBRATIION_ROUND,
    CONF_CALIBRATION,
    CONF_CHILD_LOCK,
    CONF_HEAT_AUTO_SWAPPED,
    CONF_HEATER,
    CONF_HOMATICIP,
    CONF_MODEL,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_VALVE_MAINTENANCE,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE,
    SUPPORT_FLAGS,
    VERSION,
    SERVICE_SET_TEMP_TARGET_TEMPERATURE,
    BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
    BetterThermostatEntityFeature,
)

from .controlling import control_queue, set_hvac_mode, set_target_temperature
from .events.temperature import trigger_temperature_change
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_devices):
    """Setup sensor platform."""

    async def async_service_handler(self, data):
        _LOGGER.debug(f"Service call: {self} » {data.service}")
        if data.service == SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE:
            await self.restore_temp_temperature()
        elif data.service == SERVICE_SET_TEMP_TARGET_TEMPERATURE:
            await self.set_temp_temperature(data.data[ATTR_TEMPERATURE])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_TEMP_TARGET_TEMPERATURE,
        BETTERTHERMOSTAT_SET_TEMPERATURE_SCHEMA,
        async_service_handler,
        [
            BetterThermostatEntityFeature.TARGET_TEMPERATURE,
            BetterThermostatEntityFeature.TARGET_TEMPERATURE_RANGE,
        ],
    )
    platform.async_register_entity_service(
        SERVICE_RESTORE_SAVED_TARGET_TEMPERATURE, {}, async_service_handler
    )

    async_add_devices(
        [
            BetterThermostat(
                entry.data[CONF_NAME],
                entry.data[CONF_HEATER],
                entry.data[CONF_SENSOR],
                entry.data[CONF_SENSOR_WINDOW] or None,
                entry.data[CONF_WINDOW_TIMEOUT],
                entry.data[CONF_WEATHER] or None,
                entry.data[CONF_OUTDOOR_SENSOR] or None,
                entry.data[CONF_OFF_TEMPERATURE],
                entry.data[CONF_VALVE_MAINTENANCE],
                entry.data[CONF_CALIBRATION],
                entry.data[CONF_MODEL],
                entry.data[CONF_CALIBRATIION_ROUND],
                entry.data[CONF_HEAT_AUTO_SWAPPED],
                entry.data[CONF_CHILD_LOCK],
                entry.data[CONF_HOMATICIP] or False,
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
        self._saved_temperature = self._target_temp
        self._target_temp = convert_to_float(
            temperature, self.name, "service.set_target_temperature()"
        )
        self.async_write_ha_state()
        await self.control_queue_task.put(self)

    async def save_target_temperature(self):
        self._saved_temperature = self._target_temp
        self.async_write_ha_state()

    async def restore_temp_temperature(self):
        if self._saved_temperature is not None:
            self._target_temp = convert_to_float(
                self._saved_temperature, self.name, "service.restore_temp_temperature()"
            )
            self._saved_temperature = None
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Better Thermostat",
            "model": self.model,
            "sw_version": VERSION,
            "via_device": ("climate", self.heater_entity_id),
        }

    def __init__(
        self,
        name,
        heater_entity_id,
        sensor_entity_id,
        window_id,
        window_delay,
        weather_entity,
        outdoor_sensor,
        off_temperature,
        valve_maintenance,
        calibration,
        model,
        calibration_round,
        heat_auto_swapped,
        child_lock,
        homaticip,
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
        self.heater_entity_id = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.window_id = window_id or None
        self.window_delay = window_delay or 0
        self.weather_entity = weather_entity or None
        self.outdoor_sensor = outdoor_sensor or None
        self.off_temperature = float(off_temperature) or None
        self.valve_maintenance = valve_maintenance or None
        self.model = model
        self._unique_id = unique_id
        self._unit = unit
        self._calibration = calibration
        self.local_temperature_calibration_entity = None
        self._device_class = device_class
        self._state_class = state_class
        self.calibration_round = calibration_round
        self.heat_auto_swapped = heat_auto_swapped
        self.child_lock = child_lock
        self.homaticip = homaticip
        self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
        self.next_valve_maintenance = datetime.now() + timedelta(
            hours=randint(1, 24 * 5)
        )
        self._config = None
        self._cur_temp = None
        self.window_open = None
        self._target_temp_step = 1
        self._TRV_target_temp_step = 0.5
        self.calibration_type = 1
        self._min_temp = 0
        self._max_temp = 30
        self._TRV_min_temp = 0
        self._TRV_max_temp = 30
        self._TRV_current_temp = None
        self._TRV_SUPPORTED_HVAC_MODES = None
        self._target_temp = 5
        self._support_flags = SUPPORT_FLAGS
        self._bt_hvac_mode = None
        self._trv_hvac_mode = None
        self.closed_window_triggered = False
        self.call_for_heat = True
        self.ignore_states = False
        self.last_calibration = None
        self.last_dampening_timestamp = None
        self.valve_position_entity = None
        self.version = VERSION
        self.last_change = datetime.now() - timedelta(hours=2)
        self._last_calibration = datetime.now() - timedelta(hours=2)
        self._last_window_state = None
        self._temp_lock = asyncio.Lock()
        self._last_reported_valve_position = None
        self.startup_running = True
        self._init = True
        self._saved_temperature = None
        self._last_reported_valve_position_update_wait_lock = asyncio.Lock()
        self._last_send_target_temp = None
        self._last_avg_outdoor_temp = None
        self._available = False
        self.control_queue_task = asyncio.Queue(maxsize=-1)
        if self.window_id is not None:
            self.window_queue_task = asyncio.Queue(maxsize=-1)
        asyncio.create_task(control_queue(self))
        if self.window_id is not None:
            asyncio.create_task(window_queue(self))

    async def async_added_to_hass(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        await super().async_added_to_hass()

        _LOGGER.info(
            "better_thermostat %s: Waiting for entity to be ready...", self.name
        )

        if self._calibration == "local_calibration_based":
            self.calibration_type = 0
            self.local_temperature_calibration_entity = (
                await find_local_calibration_entity(self)
            )
            _LOGGER.info(
                "better_thermostat %s: uses local calibration entity %s",
                self.name,
                self.local_temperature_calibration_entity,
            )

        @callback
        def _async_startup(*_):
            """Init on startup.

            Parameters
            ----------
            _ :
                    All parameters are piped.
            """
            loop = asyncio.get_event_loop()
            loop.create_task(self.startup())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

    async def _trigger_time(self, event=None):
        _LOGGER.debug("better_thermostat %s: get last avg outdoor temps...", self.name)
        await check_ambient_air_temperature(self)
        if event is not None:
            self.async_write_ha_state()
            await self.control_queue_task.put(self)

    async def _trigger_temperature_change(self, event):
        await trigger_temperature_change(self, event)

    async def _trigger_trv_change(self, event):
        await trigger_trv_change(self, event)

    async def _trigger_window_change(self, event):
        await trigger_window_change(self, event)

    async def startup(self):
        """Run when entity about to be added.

        Returns
        -------
        None
        """
        self._config = {
            "calibration_type": self.calibration_type,
            "calibration_round": self.calibration_round,
            "has_system_mode": False,
            "system_mode": HVAC_MODE_OFF,
            "heat_auto_swapped": self.heat_auto_swapped,
        }
        while self.startup_running:
            _LOGGER.info(
                "better_thermostat %s: Starting version %s. Waiting for entity to be ready...",
                self.name,
                self.version,
            )
            trv_state = self.hass.states.get(self.heater_entity_id)
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if sensor_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                _LOGGER.info(
                    "better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
                    self.name,
                    self.sensor_entity_id,
                )
                await asyncio.sleep(10)
                continue
            if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                _LOGGER.info(
                    "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                    self.name,
                    self.heater_entity_id,
                )
                await asyncio.sleep(10)
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

            self._trv_hvac_mode = trv_state.state
            self._last_reported_valve_position = (
                trv_state.attributes.get("valve_position", None) or None
            )
            self._max_temp = trv_state.attributes.get("max_temp", 30)
            self._min_temp = trv_state.attributes.get("min_temp", 5)
            self._TRV_max_temp = trv_state.attributes.get("max_temp", 30)
            self._TRV_min_temp = trv_state.attributes.get("min_temp", 5)
            self._TRV_SUPPORTED_HVAC_MODES = trv_state.attributes.get(
                "hvac_modes", None
            )
            self._TRV_target_temp_step = trv_state.attributes.get("target_temp_step", 1)
            self._target_temp_step = self._TRV_target_temp_step
            self._TRV_current_temp = trv_state.attributes.get("current_temperature")
            self._cur_temp = convert_to_float(
                str(sensor_state.state), self.name, "startup()"
            )
            if self.window_id is not None:
                window = self.hass.states.get(self.window_id)

                check = window.state
                _LOGGER.debug(
                    "better_thermostat %s: window sensor state is %s", self.name, check
                )
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

            has_system_mode = True
            if trv_state.attributes.get("hvac_modes") is None:
                has_system_mode = False

            self._config["has_system_mode"] = has_system_mode
            # Check If we have an old state
            old_state = await self.async_get_last_state()
            _LOGGER.debug(old_state)

            if old_state is not None:
                # If we have no initial temperature, restore
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self._target_temp = (
                        trv_state.attributes.get("current_heating_setpoint")
                        or trv_state.attributes.get("temperature")
                        or 5
                    )
                    _LOGGER.debug(
                        "better_thermostat %s: Undefined target temperature, falling back to %s",
                        self.name,
                        self._target_temp,
                    )
                else:
                    _old_target_temperature = float(
                        old_state.attributes.get(ATTR_TEMPERATURE)
                    )
                    # if the saved temperature is lower than the _min_temp, set it to _min_temp
                    if _old_target_temperature < self._min_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is lower than _min_temp %s, setting to _min_temp",
                            self.name,
                            _old_target_temperature,
                            self._min_temp,
                        )
                        _old_target_temperature = self._min_temp
                    # if the saved temperature is higher than the _max_temp, set it to _max_temp
                    elif _old_target_temperature > self._max_temp:
                        _LOGGER.warning(
                            "better_thermostat %s: Saved target temperature %s is higher than _max_temp %s, setting to _max_temp",
                            self.name,
                            _old_target_temperature,
                            self._min_temp,
                        )
                        _old_target_temperature = self._max_temp
                    self._target_temp = _old_target_temperature
                if not self._bt_hvac_mode and old_state.state:
                    self._bt_hvac_mode = old_state.state
                if not old_state.attributes.get(ATTR_STATE_LAST_CHANGE):
                    self.last_change = old_state.attributes.get(ATTR_STATE_LAST_CHANGE)
                if not old_state.attributes.get(ATTR_STATE_DAY_SET_TEMP):
                    self.last_daytime_temp = old_state.attributes.get(
                        ATTR_STATE_DAY_SET_TEMP
                    )
                if not old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT):
                    self.call_for_heat = old_state.attributes.get(
                        ATTR_STATE_CALL_FOR_HEAT
                    )
                if not old_state.attributes.get(ATTR_STATE_SAVED_TEMPERATURE):
                    self._saved_temperature = old_state.attributes.get(
                        ATTR_STATE_SAVED_TEMPERATURE
                    )
            else:
                # No previous state, try and restore defaults
                if self._target_temp is None:
                    _LOGGER.info(
                        "better_thermostat %s: No previously saved temperature found on startup, get it from the TRV",
                        self.name,
                    )
                    self._target_temp = (
                        trv_state.attributes.get("current_heating_setpoint")
                        or trv_state.attributes.get("temperature")
                        or 5
                    )
            # if hvac mode could not be restored, turn heat off
            if self._trv_hvac_mode is None:
                self._trv_hvac_mode = HVAC_MODE_OFF
            if self._bt_hvac_mode in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                _LOGGER.warning(
                    "better_thermostat %s: No previously hvac mode found on startup, turn heat off",
                    self.name,
                )
                self._bt_hvac_mode = mode_remap(self, self._trv_hvac_mode, True)

            _LOGGER.debug(
                "better_thermostat %s: Startup config, TRV hvac mode is %s, BT hvac mode is %s, Target temp %s",
                self.name,
                self._trv_hvac_mode,
                self._bt_hvac_mode,
                self._target_temp,
            )

            self._config["system_mode"] = self._bt_hvac_mode
            self._last_window_state = self.window_open
            self._available = True
            self.startup_running = False
            self.async_write_ha_state()
            await self._trigger_time(None)
            await self.control_queue_task.put(self)
            # Add listener
            if self.outdoor_sensor is not None:
                async_track_time_change(self.hass, self._trigger_time, 5, 0, 0)
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._trigger_temperature_change
            )
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._trigger_trv_change
            )
            if self.window_id is not None:
                async_track_state_change_event(
                    self.hass, [self.window_id], self._trigger_window_change
                )
            _LOGGER.info("better_thermostat %s: startup completed.", self.name)
            break

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
            CONF_CHILD_LOCK: self.child_lock,
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
        if self._target_temp_step is not None:
            return self._target_temp_step

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
        return self._cur_temp

    @property
    def hvac_mode(self):
        """Return current operation.

        Returns
        -------
        string
                HVAC mode only from homeassistant.components.climate.const is valid
        """
        return self._bt_hvac_mode

    @property
    def hvac_action(self):
        """Return the current HVAC action"""

        if self._bt_hvac_mode == HVAC_MODE_OFF:
            _LOGGER.debug(
                f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_OFF}"
            )
            return CURRENT_HVAC_OFF
        if self._bt_hvac_mode == HVAC_MODE_HEAT:
            if self.window_open:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with '{CURRENT_HVAC_OFF}' because a window is open"
                )
                return CURRENT_HVAC_OFF

            if self.call_for_heat is False:
                _LOGGER.debug(
                    f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with '{CURRENT_HVAC_IDLE}' since call for heat is false"
                )
                return CURRENT_HVAC_IDLE
            _LOGGER.debug(
                f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_HEAT}"
            )
            return CURRENT_HVAC_HEAT

    @property
    def target_temperature(self):
        """Return the temperature we try to reach.

        Returns
        -------
        float
                Target temperature.
        """
        if None in (self._max_temp, self._min_temp, self._target_temp):
            return self._target_temp
        # if target temp is below minimum, return minimum
        if self._target_temp < self._min_temp:
            return self._min_temp
        # if target temp is above maximum, return maximum
        if self._target_temp > self._max_temp:
            return self._max_temp
        return self._target_temp

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
        await set_hvac_mode(self, hvac_mode)

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
        await set_target_temperature(self, **kwargs)

    @property
    def min_temp(self):
        """Return the minimum temperature.

        Returns
        -------
        float
                the minimum temperature.
        """
        if self._min_temp is not None:
            return self._min_temp

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
        if self._max_temp is not None:
            return self._max_temp

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
        state_off = self.hass.states.is_state(self.heater_entity_id, "off")
        state_heat = self.hass.states.is_state(self.heater_entity_id, "heat")
        state_auto = self.hass.states.is_state(self.heater_entity_id, "auto")

        if not self.hass.states.get(self.heater_entity_id):
            return None
        if state_off:
            return False
        elif state_heat:
            return state_heat
        elif state_auto:
            return state_auto

    @property
    def supported_features(self):
        """Return the list of supported features.

        Returns
        -------
        array
                Supported features.
        """
        return self._support_flags
