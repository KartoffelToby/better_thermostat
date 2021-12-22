"""Special support for AI thermostat units.
Z2M version """

import asyncio
import json
import logging
from abc import ABC
from datetime import datetime, timedelta

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
	CURRENT_HVAC_HEAT,
	CURRENT_HVAC_IDLE,
	CURRENT_HVAC_OFF,
	HVAC_MODE_HEAT,
	HVAC_MODE_OFF,
	SUPPORT_TARGET_TEMPERATURE,
	SERVICE_SET_TEMPERATURE,
	SERVICE_SET_HVAC_MODE,
)
from homeassistant.components.recorder import history
from homeassistant.const import (
	ATTR_ENTITY_ID,
	ATTR_TEMPERATURE,
	CONF_NAME,
	CONF_UNIQUE_ID,
	EVENT_HOMEASSISTANT_START,
	STATE_UNAVAILABLE,
	STATE_UNKNOWN,
)
from homeassistant.core import CoreState, callback
from homeassistant.helpers.event import (
	async_track_state_change_event,
	async_track_time_change
)
from homeassistant.helpers.json import JSONEncoder
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.ai_thermostat.helpers import check_float, convert_decimal
from custom_components.ai_thermostat.models.models import convert_inbound_states, convert_outbound_states
from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "AI Thermostat"

CONF_HEATER = "thermostat"
CONF_SENSOR = "temperature_sensor"
CONF_SENSOR_WINDOW = "window_sensors"
CONF_TARGET_TEMP = "target_temp"
CONF_WEATHER = "weather"
CONF_OFF_TEMPERATURE = "off_temperature"
CONF_WINDOW_TIMEOUT = "window_off_delay"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_VALVE_MAINTENANCE = "valve_maintenance"
CONF_NIGHT_TEMP = "night_temp"
CONF_NIGHT_START = "night_start"
CONF_NIGHT_END = "night_end"

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

ATTR_STATE_WINDOW_OPEN = "window_open"
ATTR_STATE_NIGHT_MODE = "night_mode"
ATTR_STATE_SUMMER = "summer"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
		{
			vol.Required(CONF_HEATER)                          : cv.entity_id,
			vol.Required(CONF_SENSOR)                          : cv.entity_id,
			vol.Optional(CONF_SENSOR_WINDOW)                   : cv.entity_id,
			vol.Optional(CONF_WEATHER)                         : cv.entity_id,
			vol.Optional(CONF_OUTDOOR_SENSOR)                  : cv.entity_id,
			vol.Optional(CONF_OFF_TEMPERATURE, default=20)     : vol.Coerce(int),
			vol.Optional(CONF_WINDOW_TIMEOUT, default=0)       : vol.Coerce(int),
			vol.Optional(CONF_VALVE_MAINTENANCE, default=False): cv.boolean,
			vol.Optional(CONF_NIGHT_TEMP, default=-1)          : vol.Coerce(int),
			vol.Optional(CONF_NIGHT_START, default='23:00')    : cv.string,
			vol.Optional(CONF_NIGHT_END, default='05:00')      : cv.string,
			vol.Optional(CONF_NAME, default=DEFAULT_NAME)      : cv.string,
			vol.Optional(CONF_TARGET_TEMP)                     : vol.Coerce(float),
			vol.Optional(CONF_UNIQUE_ID)                       : cv.string,
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
	weather_entity = config.get(CONF_WEATHER)
	outdoor_sensor = config.get(CONF_OUTDOOR_SENSOR)
	off_temperature = config.get(CONF_OFF_TEMPERATURE)
	valve_maintenance = config.get(CONF_VALVE_MAINTENANCE)
	night_temp = config.get(CONF_NIGHT_TEMP)
	night_start = config.get(CONF_NIGHT_START)
	night_end = config.get(CONF_NIGHT_END)
	
	min_temp = 5.0
	max_temp = 30.0
	target_temp = config.get(CONF_TARGET_TEMP)
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
						weather_entity,
						outdoor_sensor,
						off_temperature,
						valve_maintenance,
						night_temp,
						night_start,
						night_end,
						min_temp,
						max_temp,
						target_temp,
						precision,
						unit,
						unique_id,
						device_class="better_thermostat",
						state_class="better_thermostat_state",
				)
			]
	)


class AIThermostat(ClimateEntity, RestoreEntity, ABC):
	"""Representation of an AI Thermostat device."""
	
	def __init__(
			self,
			mqtt,
			name,
			heater_entity_id,
			sensor_entity_id,
			window_sensors_entity_ids,
			window_delay,
			weather_entity,
			outdoor_sensor,
			off_temperature,
			valve_maintenance,
			night_temp,
			night_start,
			night_end,
			min_temp,
			max_temp,
			target_temp,
			precision,
			unit,
			unique_id,
			device_class,
			state_class,
	):
		"""Initialize the thermostat."""
		self.mqtt = mqtt
		self._name = name
		self.heater_entity_id = heater_entity_id
		self.sensor_entity_id = sensor_entity_id
		self.window_sensors_entity_ids = window_sensors_entity_ids
		self.window_delay = window_delay or 0
		self.weather_entity = weather_entity
		self.outdoor_sensor = outdoor_sensor
		self.off_temperature = off_temperature
		self.valve_maintenance = valve_maintenance
		self.night_temp = night_temp
		self.night_start = dt_util.parse_time(night_start)
		self.night_end = dt_util.parse_time(night_end)
		self._hvac_mode = HVAC_MODE_HEAT
		self._saved_target_temp = target_temp or 5.0
		self._target_temp_step = precision
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
		self.window_open = False
		self._is_away = False
		self.startup_running = True
		self.heating_active_pre_window_opened = None
		self.model = "-"
		self.next_valve_maintenance = datetime.now() + timedelta(days=5)
		self.calibration_type = 2
		self.daytime_temp = 5
		self.closed_window_triggered = False
		self.night_status = False
		self.summer = False
		self.ignore_states = False
		self.last_calibration = datetime.now() - timedelta(minutes=5)
		self.lastOverswing = datetime.now()
		self._device_class = device_class
		self._state_class = state_class
		self._today_nightmode_end = datetime.now()
	
	async def async_added_to_hass(self):
		"""Run when entity about to be added."""
		await super().async_added_to_hass()
		
		# Add listener
		async_track_state_change_event(
				self.hass, [self.sensor_entity_id], self._async_sensor_changed
		)
		async_track_state_change_event(
				self.hass, [self.heater_entity_id], self._async_trv_changed
		)
		if self.window_sensors_entity_ids:
			async_track_state_change_event(
					self.hass, [self.window_sensors_entity_ids], self._async_window_changed
			)
		
		if self.night_temp != -1:
			async_track_time_change(
					self.hass,
					self._async_timer_trigger,
					self.night_start.hour,
					self.night_start.minute,
					self.night_start.second,
			)
			async_track_time_change(
					self.hass,
					self._async_timer_trigger,
					self.night_end.hour,
					self.night_end.minute,
					self.night_end.second,
			)
		
		@callback
		def _async_startup(*_):
			"""Init on startup."""
			
			_LOGGER.info("Starting ai_thermostat for %s with version: 0.8.6 waiting for entity to be ready...", self.name)
			
			loop = asyncio.get_event_loop()
			loop.create_task(self.startup())
		
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
	
	async def startup(self):
		await asyncio.sleep(5)
		
		while self.startup_running:
			sensor_state = self.hass.states.get(self.sensor_entity_id)
			trv_state = self.hass.states.get(self.heater_entity_id)
			
			if sensor_state is None:
				_LOGGER.error("ai_thermostat %s temperature sensor: %s is not in HA or wrong spelled", self.name, self.sensor_entity_id)
				return False
			if trv_state is None:
				_LOGGER.error("ai_thermostat %s TRV: %s is not in HA or wrong spelled", self.name, self.heater_entity_id)
				return False
			if self.window_sensors_entity_ids:
				window = self.hass.states.get(self.window_sensors_entity_ids)
				
				if window is None:
					_LOGGER.error("ai_thermostat %s window sensor: %s is not in HA or wrong spelled", self.name, self.window_sensors_entity_ids)
					return False
			
			_ready = True
			
			if sensor_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info("ai_thermostat %s still waiting for %s to be available", self.name, self.sensor_entity_id)
				_ready = False
			if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info("ai_thermostat %s still waiting for %s to be available", self.name, self.heater_entity_id)
				_ready = False
			
			if self.hass.states.get(self.heater_entity_id).attributes.get('device') is None:
				_LOGGER.info("ai_thermostat %s still waiting for %s to be available", self.name, self.heater_entity_id)
				_ready = False
			
			if self.window_sensors_entity_ids and window.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info("ai_thermostat %s still waiting for %s to be available", self.name, self.window_sensors_entity_ids)
				_ready = False
			
			if not _ready:
				_LOGGER.info("retry in 15s...")
				await asyncio.sleep(15)
				continue
			
			if self.window_sensors_entity_ids:
				window = self.hass.states.get(self.window_sensors_entity_ids)
				
				check = window.state
				if check == 'on':
					self.window_open = True
					self._hvac_mode = HVAC_MODE_OFF
				else:
					self.window_open = False
					self.closed_window_triggered = False
				_LOGGER.debug("ai_thermostat: Window %s", self.window_open)
			
			_LOGGER.info("Register ai_thermostat with name: %s", self.name)
			self.startup_running = False
			self._active = True
			self._async_update_temp(sensor_state)
			self.async_write_ha_state()
			await asyncio.sleep(5)
			await self._async_control_heating()
		
		return True
	
	@property
	def device_class(self):
		return self._device_class
	
	@property
	def extra_state_attributes(self):
		"""Return the device specific state attributes."""
		dev_specific = {
			ATTR_STATE_WINDOW_OPEN: self.window_open,
			ATTR_STATE_NIGHT_MODE : self.night_status,
			ATTR_STATE_SUMMER     : self.summer
		}
		
		return dev_specific
	
	@property
	def available(self):
		"""Return if thermostat is available."""
		return not self.startup_running
	
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
		return super().precision
	
	@property
	def target_temperature_step(self):
		"""Return the supported step of target temperature."""
		if self._target_temp_step is not None:
			return self._target_temp_step
		
		return super().precision
	
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
		
		try:
			if self.hass.states.get(self.heater_entity_id).attributes.get('position') is not None:
				if check_float(self.hass.states.get(self.heater_entity_id).attributes.get('position')):
					valve = float(self.hass.states.get(self.heater_entity_id).attributes.get('position'))
					if valve > 0:
						return CURRENT_HVAC_HEAT
					else:
						return CURRENT_HVAC_IDLE
			
			if self.hass.states.get(self.heater_entity_id).attributes.get('pi_heating_demand') is not None:
				if check_float(self.hass.states.get(self.heater_entity_id).attributes.get('pi_heating_demand')):
					valve = float(self.hass.states.get(self.heater_entity_id).attributes.get('pi_heating_demand'))
					if valve > 0:
						return CURRENT_HVAC_HEAT
					else:
						return CURRENT_HVAC_IDLE
		except RuntimeError:
			_LOGGER.debug("ai_thermostat: currently can't get the TRV")
		
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
		if hvac_mode == HVAC_MODE_HEAT:
			self._hvac_mode = HVAC_MODE_HEAT
		elif hvac_mode == HVAC_MODE_OFF:
			self._hvac_mode = HVAC_MODE_OFF
		else:
			_LOGGER.debug("Unrecognized hvac mode: %s", hvac_mode)
		self.async_write_ha_state()
		if self.closed_window_triggered or self.ignore_states:
			return
		await self._async_control_heating()
	
	async def async_set_temperature(self, **kwargs):
		"""Set new target temperature."""
		temperature = kwargs.get(ATTR_TEMPERATURE)
		if temperature is None:
			return
		self._target_temp = temperature
		self.async_write_ha_state()
		if self.closed_window_triggered or self.ignore_states:
			return
		await self._async_control_heating()
	
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
	
	@callback
	async def _async_timer_trigger(self, state):
		
		if self.night_start.hour == state.hour and self.night_start.minute == state.minute:
			_LOGGER.debug("night mode active override with: %s", float(self.night_temp))
			self.daytime_temp = self._target_temp
			self._target_temp = float(self.night_temp)
			self.night_status = True
		
		if self.night_end.hour == state.hour and self.night_end.minute == state.minute:
			_LOGGER.debug("night mode inactive override with: %s", float(self.daytime_temp))
			self._target_temp = self.daytime_temp
			self.night_status = False
		
		await self._async_control_heating()
	
	@callback
	async def _async_window_changed(self, state):
		if self.startup_running:
			return
		if self.hass.states.get(self.heater_entity_id) is not None:
			await asyncio.sleep(int(self.window_delay))
			check = self.hass.states.get(self.window_sensors_entity_ids).state
			if check == 'on':
				self.window_open = True
			else:
				self.window_open = False
				self.closed_window_triggered = False
			_LOGGER.debug("ai_thermostat: Window %s", self.window_open)
			self.async_write_ha_state()
			await self._async_control_heating()
	
	@callback
	async def _async_sensor_changed(self, event):
		"""Handle temperature changes."""
		if self.startup_running:
			return
		new_state = event.data.get("new_state")
		if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
			return
		
		self._async_update_temp(new_state)
		self.async_write_ha_state()
		if self.closed_window_triggered or self.ignore_states:
			return
		await self._async_control_heating()
	
	@callback
	def _async_update_temp(self, state):
		"""Update thermostat with the latest state from sensor."""
		try:
			if check_float(state.state):
				self._cur_temp = convert_decimal(state.state)
		except ValueError as ex:
			_LOGGER.debug("Unable to update from sensor: %s", ex)
	
	@callback
	async def _async_trv_changed(self, event):
		if self.startup_running:
			return
		
		old_state = event.data.get("old_state")
		new_state = event.data.get("new_state")
		
		if new_state is None or old_state is None:
			return
		
		try:
			if self.hass.states.get(self.heater_entity_id).attributes.get('device') is not None:
				self.model = self.hass.states.get(self.heater_entity_id).attributes.get('device').get('model')
			else:
				_LOGGER.debug("ai_thermostat: can't read the device model of TRV, Enable include_device_information in z2m or checkout issue #1")
		except RuntimeError:
			_LOGGER.debug("ai_thermostat: error can't get the TRV model")
		
		if new_state.attributes is not None:
			try:
				remappedstate = convert_inbound_states(self, new_state.attributes)
				
				if old_state.attributes.get('system_mode') != new_state.attributes.get('system_mode'):
					self._hvac_mode = remappedstate.system_mode
					
					if self._hvac_mode != HVAC_MODE_OFF and self.window_open:
						self._hvac_mode = HVAC_MODE_OFF
						_LOGGER.debug("ai_thermostat: Window is still open, turn force off the TRV")
						await self._async_control_heating()
				
				if not self.ignore_states and new_state.attributes.get('current_heating_setpoint') is not None and self._hvac_mode != HVAC_MODE_OFF and self.calibration_type == 0:
					self._target_temp = new_state.attributes.get('current_heating_setpoint')
			
			except TypeError as e:
				_LOGGER.debug("ai_thermostat entity not ready or device is currently not supported %s", e)
			
			# _LOGGER.debug("ai_thermostat %s something changed W: %s | %s - %s | %s - %s",new_state.attributes.get('device').get('friendlyName'),self.window_open,new_state.attributes.get('system_mode'),old_state.attributes.get('system_mode'),new_state.attributes.get('current_heating_setpoint'),old_state.attributes.get('current_heating_setpoint'))
			self.async_write_ha_state()
	
	async def trv_valve_maintenance(self):
		self.ignore_states = True
		if self.hass.states.get(self.heater_entity_id).attributes.get('valve_position'):
			mqtt_trv_valve = {"valve_position": 255}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
			mqtt_trv_valve = {"valve_position": 0}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
			mqtt_trv_valve = {"valve_position": 255}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
			mqtt_trv_valve = {"valve_position": 0}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(5)
		else:
			mqtt_trv_valve = {"current_heating_setpoint": 30}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
			mqtt_trv_valve = {"current_heating_setpoint": 5}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
			mqtt_trv_valve = {"current_heating_setpoint": 30}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
			mqtt_trv_valve = {"current_heating_setpoint": 5}
			payload = json.dumps(mqtt_trv_valve, cls=JSONEncoder)
			await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
			await asyncio.sleep(60)
		self.ignore_states = False
		self._async_control_heating()
	
	async def _async_control_heating(self):
		if self.ignore_states or self.startup_running:
			return
		async with self._temp_lock:
			if None not in (
					self._cur_temp,
					self._target_temp,
					self._hvac_mode,
			) and self.hass.states.get(self.heater_entity_id).attributes is not None and not self.startup_running:
				self._active = True
				self.ignore_states = True
				# Use the same precision and min and max as the TRV
				if self.hass.states.get(self.heater_entity_id).attributes.get('target_temp_step') is not None:
					self._target_temp_step = float(self.hass.states.get(self.heater_entity_id).attributes.get('target_temp_step'))
				else:
					self._target_temp_step = 1
				if self.hass.states.get(self.heater_entity_id).attributes.get('min_temp') is not None:
					self._min_temp = float(self.hass.states.get(self.heater_entity_id).attributes.get('min_temp'))
				else:
					self._min_temp = 5
				if self.hass.states.get(self.heater_entity_id).attributes.get('max_temp') is not None:
					self._max_temp = float(self.hass.states.get(self.heater_entity_id).attributes.get('max_temp'))
				else:
					self._max_temp = 30
				
				"""
				# Need to force the local_temperature_calibration get updated in HA only for SPZB0001
				if(self.model == "SPZB0001"):
					mqtt_get = {"local_temperature_calibration": ""}
					payload = json.dumps(mqtt_get, cls=JSONEncoder)
					await self.mqtt.async_publish(self.hass,'zigbee2mqtt/'+self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName')+'/get', payload, 0, False)
					await asyncio.sleep(
						1 #5
					)
				"""
				
				# Get the forecast from the weather entity for two days in a row and round and split it for compare
				is_cold = self.check_if_is_winter()
				
				if not is_cold:
					self.summer = True
				else:
					self.summer = False
				
				# window open detection and weather detection force turn TRV off
				if (self.window_open or not is_cold) and not self.closed_window_triggered:
					self.heating_active_pre_window_opened = False
					if self._hvac_mode == HVAC_MODE_HEAT:
						self.heating_active_pre_window_opened = True
					self._hvac_mode = HVAC_MODE_OFF
					self.closed_window_triggered = True
				elif self.heating_active_pre_window_opened:
					self._hvac_mode = HVAC_MODE_HEAT
				
				# NEW SPECIAL STUFF :)
				try:
					remapped_states = convert_outbound_states(self, self._hvac_mode)
					converted_hvac_mode = remapped_states.get('system_mode')
					current_heating_setpoint = remapped_states.get('current_temperature')
					has_real_mode = True if self.hass.states.get(self.heater_entity_id).attributes.get('system_mode') is not None else False
					calibration = remapped_states.get('calibration')
					
					# if off do nothing
					if self.closed_window_triggered or self._hvac_mode == HVAC_MODE_OFF:
						if has_real_mode:
							if self.hass.states.get(self.heater_entity_id).attributes.get('system_mode') == HVAC_MODE_OFF:
								self.ignore_states = False
								return
						if self.calibration_type == 1:
							if float(self.hass.states.get(self.heater_entity_id).attributes.get('current_heating_setpoint')) <= 5.0:
								self.ignore_states = False
								return
					
					# Only send the local_temperature_calibration if not instantly following
					do_calibration = False
					if (datetime.now() > (self.last_calibration + timedelta(seconds=20))):
						do_calibration = True
						self.last_calibration = datetime.now()
					
					_LOGGER.debug(
							"ai_thermostat triggered states > window open: %s night mode: %s Mode: %s set: %s has_mode: %s Calibration: %s - send: %s set_temp: %s cur_temp: %s Model: %s Calibration "
							"type: %s winter: %s TRV: %s",
							self.window_open,
							self.night_status,
							converted_hvac_mode,
							self._hvac_mode,
							has_real_mode,
							calibration,
							do_calibration,
							current_heating_setpoint,
							self._cur_temp,
							self.model,
							self.calibration_type,
							is_cold,
							self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName')
					)
					
					if self.calibration_type == 1 and float(self.hass.states.get(self.heater_entity_id).attributes.get('current_heating_setpoint')) != float(calibration):
						current_heating_setpoint = calibration
						mqtt_settemp = {"current_heating_setpoint": float(calibration)}
						payload = json.dumps(mqtt_settemp, cls=JSONEncoder)
						await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
						data = {ATTR_ENTITY_ID: self.heater_entity_id, "temperature": float(calibration)}
						await self.hass.services.async_call('climate', SERVICE_SET_TEMPERATURE, data)
						await asyncio.sleep(2)
					
					if self.calibration_type == 0 and not self.window_open and self.hass.states.get(self.heater_entity_id).attributes.get('current_heating_setpoint') != float(
							current_heating_setpoint) and converted_hvac_mode != HVAC_MODE_OFF and float(current_heating_setpoint) != 5.0 and is_cold:
						mqtt_settemp = {"current_heating_setpoint": float(current_heating_setpoint)}
						payload = json.dumps(mqtt_settemp, cls=JSONEncoder)
						await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
						data = {ATTR_ENTITY_ID: self.heater_entity_id, "temperature": float(current_heating_setpoint)}
						await self.hass.services.async_call('climate', SERVICE_SET_TEMPERATURE, data)
						await asyncio.sleep(2)
					
					# Calibration stuff
					if self.calibration_type == 0 and not self.window_open and do_calibration:
						mqtt_calibration = {"local_temperature_calibration": calibration}
						payload = json.dumps(mqtt_calibration, cls=JSONEncoder)
						await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
						await asyncio.sleep(2)
					
					if has_real_mode and (converted_hvac_mode != self.hass.states.get(self.heater_entity_id).attributes.get('system_mode') or converted_hvac_mode == HVAC_MODE_OFF):
						mqtt_sys_mode = {"system_mode": converted_hvac_mode}
						payload = json.dumps(mqtt_sys_mode, cls=JSONEncoder)
						await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
						data = {ATTR_ENTITY_ID: self.heater_entity_id, "hvac_mode": converted_hvac_mode}
						await self.hass.services.async_call('climate', SERVICE_SET_HVAC_MODE, data)
					
					# Make sure it's turned off!
					if (self._hvac_mode == HVAC_MODE_OFF or self.window_open or not is_cold) and has_real_mode:
						mqtt_sys_mode = {"system_mode": HVAC_MODE_OFF}
						payload = json.dumps(mqtt_sys_mode, cls=JSONEncoder)
						await self.mqtt.async_publish(self.hass, 'zigbee2mqtt/' + self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName') + '/set', payload, 0, False)
						data = {ATTR_ENTITY_ID: self.heater_entity_id, "hvac_mode": converted_hvac_mode}
						await self.hass.services.async_call('climate', SERVICE_SET_HVAC_MODE, data)
					
					await asyncio.sleep(2)
					self.ignore_states = False
					
					# Check if a valve_maintenance is needed
					if self.valve_maintenance:
						current_time = datetime.now()
						if current_time > self.next_valve_maintenance:
							_LOGGER.debug("ai_thermostat: valve_maintenance triggerd")
							await self.trv_valve_maintenance()
							self.next_valve_maintenance = datetime.now() + timedelta(days=5)
				
				except TypeError as fatal:
					_LOGGER.debug("ai_thermostat entity not ready or device is currently not supported")
					_LOGGER.debug("fatal %s", fatal)
					self.ignore_states = False
	
	def check_if_is_winter(self):
		if self.weather_entity is not None:
			try:
				forcast = self.hass.states.get(self.weather_entity).attributes.get('forecast')
				if len(forcast) > 0:
					max_forcast_temp = int(round(float(forcast[0]['temperature']) + float(forcast[1]['temperature']) / 2))
					return max_forcast_temp < self.off_temperature
				else:
					_LOGGER.warning("ai_thermostat: no weather entity data found.")
					return True
			except TypeError:
				_LOGGER.warning("ai_thermostat: no weather entity data found.")
				return True
		
		elif self.outdoor_sensor is not None:
			# Get the HA History of our sensor for the last two days.
			last_two_days_date_time = datetime.now() - timedelta(days=2)
			start = dt_util.as_utc(last_two_days_date_time)
			history_list = history.state_changes_during_period(
					self.hass, start, dt_util.as_utc(datetime.now()), self.outdoor_sensor
			)
			
			# calculate the avg temp from the sensor data of the last two days to avoid peaks
			found_history = history_list.get(self.outdoor_sensor)
			max_found_states = 0
			max_temp = 0
			for item in found_history:
				if item.state is not None and check_float(item.state):
					max_temp += int(round(float(item.state)))
					max_found_states += 1
			
			avg_temp = int(round(float(max_temp / max_found_states)))
			_LOGGER.debug("ai_thermostat: avg outdoor temp: %s", avg_temp)
			return avg_temp < self.off_temperature
		else:
			return True
	
	@property
	def _is_device_active(self):
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
		"""Return the list of supported features."""
		return self._support_flags
