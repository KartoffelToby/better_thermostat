"""Special support for AI thermostat units."""
""" Z2M version """
import asyncio
import logging
import json
import homeassistant.util.dt as dt_util
from datetime import datetime, timedelta

import voluptuous as vol
from custom_components.ai_thermostat.models.models import convert_inbound_states, convert_outbound_states
from homeassistant.helpers.json import JSONEncoder

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity

from homeassistant.components.recorder import history


from homeassistant.components.climate.const import (
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, CoreState, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "AI Thermostat"

CONF_HEATER = "thermostat"
CONF_SENSOR = "temperature_sensor"
CONF_SENSOR_WINDOW = "window_sensors"
CONF_TARGET_TEMP = "target_temp"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
CONF_AWAY_TEMP = "away_temp"
CONF_WEATHER = "weather"
CONF_OFF_TEMPERATURE = "off_temperature"
CONF_WINDOW_TIMEOUT = "window_off_delay"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_VALVE_MAINTENANCE = "valve_maintenance"
SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HEATER): cv.entity_id,
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Required(CONF_SENSOR_WINDOW): cv.entity_id,
        vol.Optional(CONF_WEATHER): cv.entity_id,
        vol.Optional(CONF_OUTDOOR_SENSOR): cv.entity_id,
        vol.Optional(CONF_OFF_TEMPERATURE, default=20): vol.Coerce(int),
        vol.Optional(CONF_WINDOW_TIMEOUT, default=0): vol.Coerce(int),
        vol.Optional(CONF_VALVE_MAINTENANCE, default=False): cv.boolean,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In([HVAC_MODE_HEAT, HVAC_MODE_OFF]),
        vol.Optional(CONF_AWAY_TEMP): vol.Coerce(float),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the AI thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
    mqtt = hass.components.mqtt
    name = config.get(CONF_NAME)
    heater_entity_id = config.get(CONF_HEATER)
    sensor_entity_id = config.get(CONF_SENSOR)
    window_sensors_entity_ids = config.get(CONF_SENSOR_WINDOW)
    window_delay = config.get(CONF_WINDOW_TIMEOUT)
    weather = config.get(CONF_WEATHER)
    outdoor_sensor = config.get(CONF_OUTDOOR_SENSOR)
    off_temperature = config.get(CONF_OFF_TEMPERATURE)
    valve_maintenance = config.get(CONF_VALVE_MAINTENANCE)
    min_temp = 5.0
    max_temp = 30.0
    target_temp = config.get(CONF_TARGET_TEMP)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    away_temp = config.get(CONF_AWAY_TEMP)
    precision = 0.5
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)
    async_add_entities(
        [
            AIThermostat(
                mqtt,
                name,
                heater_entity_id,
                sensor_entity_id,
                window_sensors_entity_ids,
                window_delay,
                weather,
                outdoor_sensor,
                off_temperature,
                valve_maintenance,
                min_temp,
                max_temp,
                target_temp,
                initial_hvac_mode,
                away_temp,
                precision,
                unit,
                unique_id,
            )
        ]
    )


class AIThermostat(ClimateEntity, RestoreEntity):
    """Representation of a AI Thermostat device."""

    def __init__(
        self,
        mqtt,
        name,
        heater_entity_id,
        sensor_entity_id,
        window_sensors_entity_ids,
        window_delay,
        weather,
        outdoor_sensor,
        off_temperature,
        valve_maintenance,
        min_temp,
        max_temp,
        target_temp,
        initial_hvac_mode,
        away_temp,
        precision,
        unit,
        unique_id,
    ):
        """Initialize the thermostat."""
        self.mqtt = mqtt
        self._name = name
        self.heater_entity_id = heater_entity_id
        self.sensor_entity_id = sensor_entity_id
        self.window_sensors_entity_ids = window_sensors_entity_ids
        self.window_delay = window_delay
        self.weather = weather
        self.outdoor_sensor = outdoor_sensor
        self.off_temperature = off_temperature
        self.valve_maintenance = valve_maintenance
        self._hvac_mode = initial_hvac_mode
        self._saved_target_temp = target_temp or away_temp
        self._temp_precision = precision
        self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
        self._active = False
        self._cur_temp = None
        self._temp_lock = asyncio.Lock()
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._target_temp = target_temp
        self._unit = unit
        self._unique_id = unique_id
        self._support_flags = SUPPORT_FLAGS
        if away_temp:
            self._support_flags = SUPPORT_FLAGS
        self._away_temp = away_temp
        self.window_open = False
        self._is_away = False
        self.startup = True
        self.beforeClosed = HVAC_MODE_OFF
        self.model = "-"
        self.internalTemp = 0
        self.next_valve_maintenance = datetime.now() + timedelta(days = 5)
        self.isDoingMaintenance = False
        self.calibration_type = 2

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._async_sensor_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_tvr_changed
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.window_sensors_entity_ids], self._async_window_changed
            )
        )
        @callback
        def _async_startup(*_):
            """Init on startup."""
            sensor_state = self.hass.states.get(self.sensor_entity_id)
            trv_state = self.hass.states.get(self.heater_entity_id)
            window_state = self.hass.states.get(self.window_sensors_entity_ids)

            if sensor_state and sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ) and trv_state and trv_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ) and window_state and window_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ) and self.startup:
                self.startup = False
                if self.hass.states.get(self.heater_entity_id).attributes.get('local_temperature_calibration') is not None:
                    mqtt_calibration = {"local_temperature_calibration": 0}
                    payload = json.dumps(mqtt_calibration, cls=JSONEncoder)
                    self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
                _LOGGER.debug(
                    "Register ai_thermostat: %s v0.6.1",
                    self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name'),
                )
                self._async_update_temp(sensor_state)
                self.async_write_ha_state()

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            # If we have no initial temperature, restore
            if self._target_temp is None:
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    self._target_temp = self.min_temp
                    _LOGGER.debug(
                        "Undefined target temperature, falling back to %s",
                        self._target_temp,
                    )
                else:
                    self._target_temp = float(old_state.attributes[ATTR_TEMPERATURE])
            if not self._hvac_mode and old_state.state:
                self._hvac_mode = old_state.state

        else:
            # No previous state, try and restore defaults
            if self._target_temp is None:
                self._target_temp = self.min_temp
            _LOGGER.debug(
                "No previously saved temperature, setting to %s", self._target_temp
            )

        # Set default state to off
        if not self._hvac_mode:
            self._hvac_mode = HVAC_MODE_OFF


    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this thermostat."""
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self):
        """Return the sensor temperature."""
        return self._cur_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVAC_MODE_OFF:
            return CURRENT_HVAC_OFF
        if not self._is_device_active:
            return CURRENT_HVAC_IDLE
        return CURRENT_HVAC_HEAT

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._hvac_list

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        if hvac_mode == 'heat':
            self._hvac_mode = HVAC_MODE_HEAT
            await self._async_control_heating()
        elif hvac_mode == 'off':
            self._hvac_mode = HVAC_MODE_OFF
            await self._async_control_heating()
        else:
            _LOGGER.debug("Unrecognized hvac mode: %s", hvac_mode)
            await self._async_control_heating()
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temp = temperature
        await self._async_control_heating()
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if self._min_temp is not None:
            return self._min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self._max_temp is not None:
            return self._max_temp

        # Get default temp from super class
        return super().max_temp

    async def _async_sensor_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._async_update_temp(new_state)
        await self._async_control_heating()
        self.async_write_ha_state()

    @callback
    async def _async_tvr_changed(self, event):
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        if new_state is None or old_state is None:
            return

        try:
            if self.hass.states.get(self.heater_entity_id).attributes.get('device') is not None:
                self.model = self.hass.states.get(self.heater_entity_id).attributes.get('device').get('model')
            else:
                _LOGGER.debug("ai_thermostat: can't read the device model of TVR, Enable include_device_information in z2m or checkout issue #1")
        except RuntimeError:
            _LOGGER.debug("ai_thermostat: error can't get the TRV model")

        if new_state.attributes is not None:
            try:
                remappedstate = convert_inbound_states(self,new_state.attributes)
                self._hvac_mode  = remappedstate.system_mode
            except TypeError:
                _LOGGER.debug("ai_thermostat entity not ready")

        if new_state.attributes.get('current_heating_setpoint') is not None and self._hvac_mode is not HVAC_MODE_OFF and self.calibration_type == 0:
            self._target_temp = new_state.attributes.get('current_heating_setpoint')

        self.async_write_ha_state()


    async def trv_valve_maintenance(self):
        self.isDoingMaintenance = True
        if self.hass.states.get(self.heater_entity_id).attributes.get('trv_mode') is not None and self.hass.states.get(self.heater_entity_id).attributes.get('valve_position'):
            mqtt_trv_mode = {"trv_mode": 1}
            payload = json.dumps(mqtt_trv_mode, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(5)
            mqtt_trv_valve = {"valve_position": 255}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(60)
            mqtt_trv_valve = {"valve_position": 0}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(60)
            mqtt_trv_valve = {"valve_position": 255}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(60)
            mqtt_trv_valve = {"valve_position": 0}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(5)
            mqtt_trv_mode = {"trv_mode": 2}
            payload = json.dumps(mqtt_trv_mode, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
        else:
            mqtt_trv_valve = {"current_heating_setpoint": 30}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(60)
            mqtt_trv_valve = {"current_heating_setpoint": 5}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(60)
            mqtt_trv_valve = {"current_heating_setpoint": 30}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(60)
            mqtt_trv_valve = {"current_heating_setpoint": float(self._target_temp)}
            payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
            await self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
            await asyncio.sleep(5)
        self.isDoingMaintenance = False
        self._async_control_heating()

    async def window_closed_timer(self,isopen):
        await asyncio.sleep(self.window_delay)
        await asyncio.sleep(1)
        check = self.hass.states.get(self.window_sensors_entity_ids).state
        _LOGGER.debug("ai_thermostat window: %s %s",check,isopen)
        if check == 'off' and isopen == 'off':
            self.window_open = True
            await self._async_control_heating()
        else:
            self.window_open = False
            await self._async_control_heating()

    def check_float(self,potential_float):
        try:
            float(potential_float)
            return True
        except ValueError:
            return False

    @callback
    async def _async_window_changed(self, state):
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(asyncio.gather(
                self.window_closed_timer(state.data.get("new_state").state)
            ))
            loop.close()
        except RuntimeError:
            _LOGGER.debug("window is quickly openclosed")
        """
        if self.window_delay == 0:
            new_state = state.data.get("new_state")
            if new_state.state == 'off':
                self.window_open = True
                await self._async_control_heating()
            else:
                self.window_open = False
                await self._async_control_heating()
        else:
            try:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(asyncio.gather(
                    self.window_closed_timer(state.data.get("new_state").state)
                ))
                loop.close()
            except RuntimeError:
                _LOGGER.debug("window is quickly openclosed")
        """


    @callback
    def _async_update_temp(self, state):
        """Update thermostat with latest state from sensor."""
        try:
            if self.check_float(state.state):
                self._cur_temp = int(round(float(state.state)))
        except ValueError as ex:
            _LOGGER.debug("Unable to update from sensor: %s", ex)

    async def _async_control_heating(self):
        if self.isDoingMaintenance:
            return
        async with self._temp_lock:
            if None not in (
                self._cur_temp,
                self._target_temp,
                self._hvac_mode,
                self._is_device_active,
            ) and self.hass.states.get(self.heater_entity_id).attributes is not None:
                self._active = True

                # Use the same precision and min and max as the TVR
                if self.hass.states.get(self.heater_entity_id).attributes.get('target_temp_step') is not None:
                    self._temp_precision = float(self.hass.states.get(self.heater_entity_id).attributes.get('target_temp_step'))
                else:
                    self._temp_precision = 1
                if self.hass.states.get(self.heater_entity_id).attributes.get('min_temp') is not None:
                    self._min_temp = float(self.hass.states.get(self.heater_entity_id).attributes.get('min_temp'))
                else:
                    self._min_temp = 5
                if self.hass.states.get(self.heater_entity_id).attributes.get('max_temp') is not None:
                    self._max_temp = float(self.hass.states.get(self.heater_entity_id).attributes.get('max_temp'))
                else:
                    self._max_temp = 30


                # Need to force the local_temperature_calibration get updated in HA only for SPZB0001
                if(self.model == "SPZB0001"):
                    mqtt_get = {"local_temperature_calibration": ""}
                    payload = json.dumps(mqtt_get, cls=JSONEncoder)
                    self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/get', payload, 0, False)
                    await asyncio.sleep(
                        1 #5
                    )
                # Get the forecast from the weather entity for two days in a row and round and split it for compare
                is_cold = self.check_if_is_winter()

                # Window open detection and Weather detection force turn TVR off
                converted_hvac_mode = self._hvac_mode
                if not self.window_open or not is_cold:
                    self.beforeClosed = converted_hvac_mode
                    converted_hvac_mode = HVAC_MODE_OFF
                else:
                    if self.beforeClosed != HVAC_MODE_OFF:
                        converted_hvac_mode = self.beforeClosed

                # NEW SPECIAL STUFF :)
                try:
                    remappedstates = convert_outbound_states(self,converted_hvac_mode)

                    converted_hvac_mode = remappedstates.system_mode
                    local_temperature = remappedstates.local_temperature
                    local_temperature_calibration = remappedstates.local_temperature_calibration
                    current_heating_setpoint = remappedstates.current_temperature
                    has_real_mode = remappedstates.has_real_mode
                    calibration = remappedstates.calibration

                    #new_calibration = float(round(current_temp - (local_temperature - local_temperature_calibration),1))

                    # Only send the local_temperature_calibration to z2m if it's needed to avoid bugs
                    doCalibration = False
                    if self.internalTemp != local_temperature:
                        doCalibration = True
                        self.internalTemp = local_temperature

                    _LOGGER.debug(
                        "ai_thermostat triggerd, States > Window closed: %s | Mode: %s | Setted: %s | hasmode: %s | Calibration: %s - send: %s | settemp: %s | curtemp: %s | Model: %s | Calibration type: %s | Winter: %s",
                        self.window_open,
                        converted_hvac_mode,
                        self._hvac_mode,
                        has_real_mode,
                        calibration,
                        doCalibration,
                        current_heating_setpoint,
                        self._cur_temp,
                        self.model,
                        self.calibration_type,
                        is_cold
                    )

                    if self.calibration_type == 1:
                        current_heating_setpoint = calibration
                        self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set/current_heating_setpoint', float(current_heating_setpoint), 0, False)

                    if self.calibration_type == 0 and self.hass.states.get(self.heater_entity_id).attributes.get('current_heating_setpoint') != float(current_heating_setpoint) and converted_hvac_mode != HVAC_MODE_OFF and float(current_heating_setpoint) != 5.0 and is_cold:
                        self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set/current_heating_setpoint', float(current_heating_setpoint), 0, False)
                    
                    # Calibration stuff
                    if self.calibration_type == 0:
                        if calibration != local_temperature_calibration and doCalibration:
                            if has_real_mode:
                                mqtt_calibration = {"local_temperature_calibration": calibration, "system_mode": converted_hvac_mode}
                            else:
                                mqtt_calibration = {"local_temperature_calibration": calibration}
                            payload = json.dumps(mqtt_calibration, cls=JSONEncoder)
                            self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set', payload, 0, False)
                            await asyncio.sleep(
                                1 #5
                            )                        

                    if has_real_mode and converted_hvac_mode != self.hass.states.get(self.heater_entity_id).attributes.get('system_mode'):
                        self.mqtt.async_publish('zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('friendly_name')+'/set/system_mode', converted_hvac_mode, 0, False)
                    

                    await asyncio.sleep(
                        1 #5
                    )

                    ### Check if a valve_maintenance is needed
                    if self.valve_maintenance:
                        currentTime = datetime.now()
                        if currentTime > self.next_valve_maintenance:
                            _LOGGER.debug("ai_thermostat: valve_maintenance triggerd")
                            await self.trv_valve_maintenance()
                            self.next_valve_maintenance = datetime.now() + timedelta(days = 5)

                except TypeError as fatal:
                    _LOGGER.debug("ai_thermostat entity not ready")

    def check_if_is_winter(self):
        if self.weather is not None:
            try:
                forcast = self.hass.states.get(self.weather).attributes.get('forecast')
                if len(forcast) > 0:
                    max_forcast_temp = int(round(float(forcast[0]['temperature']) + float(forcast[1]['temperature']) / 2))
                    return max_forcast_temp < self.off_temperature
                else:
                    _LOGGER.warn("ai_thermostat: no weather data found.")
                    return True
            except TypeError:
                _LOGGER.warn("ai_thermostat: no weather data found.")
                return True

        elif self.outdoor_sensor is not None:
            # Get the HA History of our sensor for the last two days.
            last_two_days_date_time = datetime.now() - timedelta(days = 2)
            start = dt_util.as_utc(last_two_days_date_time)
            history_list = history.state_changes_during_period(
                self.hass, start, None, self.outdoor_sensor
            )

            # calculate the avg temp from the sensor data of the last two days to avoid peaks
            found_history = history_list.get(self.outdoor_sensor)
            max_found_states = 0
            max_temp = 0
            for item in found_history:
                if item.state is not None and self.check_float(item.state):
                    max_temp += int(round(float(item.state)))
                    max_found_states += 1

            avg_temp = int(round(float(max_temp / max_found_states)))
            _LOGGER.debug(
                "ai_thermostat: avg outdoor temp: %s",
                avg_temp
            )
            return avg_temp < self.off_temperature
        else:
            return True

    @property
    def _is_device_active(self):
        state_off = self.hass.states.is_state(self.heater_entity_id, "off")
        state_heat = self.hass.states.is_state(self.heater_entity_id, "heat")
        state_auto = self.hass.states.is_state(self.heater_entity_id, "auto")
        #state_temp = self.hass.states.get(self.heater_entity_id)
        #_LOGGER.debug("%s.state = %s", self.heater_entity_id, state_temp)
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
        """Return the list of supported features."""
        return self._support_flags