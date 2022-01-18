"""Special support for Better Thermostat units.
Z2M version """

import asyncio
import logging
import math
import numbers
from abc import ABC
from datetime import datetime, timedelta
from random import randint

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.climate import ClimateEntity, PLATFORM_SCHEMA
from homeassistant.components.climate.const import (
	CURRENT_HVAC_HEAT, CURRENT_HVAC_IDLE, CURRENT_HVAC_OFF, HVAC_MODE_HEAT, HVAC_MODE_OFF,
	SERVICE_SET_HVAC_MODE, SUPPORT_TARGET_TEMPERATURE
)
from homeassistant.components.recorder import history
from homeassistant.const import (ATTR_TEMPERATURE, CONF_NAME, CONF_UNIQUE_ID, EVENT_HOMEASSISTANT_START, STATE_UNAVAILABLE, STATE_UNKNOWN)
from homeassistant.core import callback, CoreState
from homeassistant.helpers.entity_registry import (async_entries_for_config_entry)
from homeassistant.helpers.event import (async_track_state_change_event, async_track_time_change)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN, PLATFORMS
from .helpers import check_float, set_trv_values
from .models.models import convert_inbound_states, convert_outbound_states, get_device_model

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Better Thermostat"

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
ATTR_STATE_CALL_FOR_HEAT = "call_for_heat"
ATTR_STATE_LAST_CHANGE = "last_change"
ATTR_STATE_DAY_SET_TEMP = "last_day_set_temp"

ATTR_VALVE_POSITION = "valve_position"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
	{
		vol.Required(CONF_HEATER)                          : cv.entity_id,
		vol.Required(CONF_SENSOR)                          : cv.entity_id,
		vol.Optional(CONF_SENSOR_WINDOW)                   : cv.entity_id,
		vol.Optional(CONF_WEATHER)                         : cv.entity_id,
		vol.Optional(CONF_OUTDOOR_SENSOR)                  : cv.entity_id,
		vol.Optional(CONF_OFF_TEMPERATURE, default=20.0)   : vol.Coerce(float),
		vol.Optional(CONF_WINDOW_TIMEOUT, default=0)       : vol.Coerce(int),
		vol.Optional(CONF_VALVE_MAINTENANCE, default=False): cv.boolean,
		vol.Optional(CONF_NIGHT_TEMP, default=18.0)        : vol.Coerce(float),
		vol.Optional(CONF_NIGHT_START, default=None)       : vol.Coerce(str),
		vol.Optional(CONF_NIGHT_END, default=None)         : vol.Coerce(str),
		vol.Optional(CONF_NAME, default=DEFAULT_NAME)      : cv.string,
		vol.Optional(CONF_TARGET_TEMP)                     : vol.Coerce(float),
		vol.Optional(CONF_UNIQUE_ID)                       : cv.string, }
)


async def async_setup_platform(hass, config, async_add_entities):
	"""Set up the Better Thermostat platform."""
	
	await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
	_config_name = config.get(CONF_NAME)
	_config_heater = config.get(CONF_HEATER)
	_config_sensor = config.get(CONF_SENSOR)
	_config_sensor_window = config.get(CONF_SENSOR_WINDOW)
	_config_window_timeout = config.get(CONF_WINDOW_TIMEOUT)
	_config_weather = config.get(CONF_WEATHER)
	_config_outdoor_sensor = config.get(CONF_OUTDOOR_SENSOR)
	_config_off_temperature = config.get(CONF_OFF_TEMPERATURE)
	_config_valve_maintenance = config.get(CONF_VALVE_MAINTENANCE)
	_config_night_temp = config.get(CONF_NIGHT_TEMP)
	_config_night_start = config.get(CONF_NIGHT_START)
	_config_night_end = config.get(CONF_NIGHT_END)
	
	_minimal_set_temperature = 5.0
	_maximal_set_temperature = 30.0
	_current_temperature_setpoint = config.get(CONF_TARGET_TEMP)
	_setpoint_step = 0.5
	_ha_temperature_unit = hass.config.units.temperature_unit
	_config_unique_id = config.get(CONF_UNIQUE_ID)
	
	async_add_entities(
		[BetterThermostat(
			_config_name,
			_config_heater,
			_config_sensor,
			_config_sensor_window,
			_config_window_timeout,
			_config_weather,
			_config_outdoor_sensor,
			_config_off_temperature,
			_config_valve_maintenance,
			_config_night_temp,
			_config_night_start,
			_config_night_end,
			_minimal_set_temperature,
			_maximal_set_temperature,
			_current_temperature_setpoint,
			_setpoint_step,
			_ha_temperature_unit,
			_config_unique_id,
			device_class="better_thermostat",
			state_class="better_thermostat_state"
		)]
	)


class BetterThermostat(ClimateEntity, RestoreEntity, ABC):
	"""Representation of a Better Thermostat device."""
	
	def __init__(self, name, config_heater_entity, config_room_air_temperature_sensor, config_window_sensors_group, config_window_delay,
	             config_weather_sensor, config_outdoor_air_temperature_sensor, config_turnoff_temperature_threshold,
	             config_valve_maintenance, config_nighttime_setpoint, config_nighttime_start, config_nighttime_end, minimal_set_temperature,
	             maximal_set_temperature, current_temperature_setpoint, _setpoint_step, temperature_unit, unique_id, device_class,
	             state_class):
		"""Initialize the thermostat."""
		self._name = name
		self.config_heater_entity = config_heater_entity
		self.config_room_air_temperature_sensor = config_room_air_temperature_sensor
		self.config_window_sensors_group = config_window_sensors_group
		self.config_window_delay = config_window_delay or 0
		self.config_weather_sensor = config_weather_sensor
		self.config_outdoor_air_temperature_sensor = config_outdoor_air_temperature_sensor
		self.config_turnoff_temperature_threshold = config_turnoff_temperature_threshold or None
		self.config_valve_maintenance = config_valve_maintenance
		self.config_nighttime_setpoint = config_nighttime_setpoint or None
		self.config_nighttime_start = dt_util.parse_time(config_nighttime_start) or None
		self.config_nighttime_end = dt_util.parse_time(config_nighttime_end) or None
		self._hvac_mode = HVAC_MODE_HEAT
		self._saved_setpoint = current_temperature_setpoint or None
		self._setpoint_step = _setpoint_step
		self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
		self._current_room_temperature_sensor_value = None
		self._heating_control_lock = asyncio.Lock()
		self._minimal_set_temperature = minimal_set_temperature
		self._maximal_set_temperature = maximal_set_temperature
		self._current_temperature_setpoint = current_temperature_setpoint
		self._temperature_unit = temperature_unit
		self._unique_id = unique_id
		self._support_flags = SUPPORT_FLAGS
		self.window_open = None
		self.startup_running = True
		self.model = None
		self.next_valve_maintenance = datetime.now() + timedelta(hours=randint(1, 24 * 5))
		self.calibration_type = 2
		self.last_daytime_setpoint = None
		self.closed_window_triggered = False
		self.night_mode_active = None
		self.call_for_heat = None
		self.ignore_state_updates = False
		self.last_calibration_timestamp = None
		self.last_dampening_timestamp = None
		self._device_class = device_class
		self._state_class = state_class
		self.local_temperature_calibration_entity = None
		self.valve_position_entity = None
		self.version = "1.0.0"
		self.last_change = None
		self.load_saved_state = False
		self._last_reported_valve_position = None
		self._last_reported_valve_position_update_wait_lock = asyncio.Lock()
	
	# noinspection PyTypeChecker
	async def async_added_to_hass(self):
		"""Run when entity about to be added."""
		await super().async_added_to_hass()
		
		# Add listener
		async_track_state_change_event(
			self.hass, [self.config_room_air_temperature_sensor], self._async_sensor_changed
		)
		async_track_state_change_event(
			self.hass, [self.config_heater_entity], self._async_trv_changed
		)
		if self.config_window_sensors_group:
			async_track_state_change_event(
				self.hass, [self.config_window_sensors_group], self._async_window_changed
			)
		
		# check if night mode was configured
		if all([self.config_nighttime_start, self.config_nighttime_end, self.config_nighttime_setpoint]):
			_LOGGER.debug("better_thermostat %s: Night mode configured", self.name)
			async_track_time_change(
				self.hass,
				self._async_timer_trigger,
				self.config_nighttime_start.hour,
				self.config_nighttime_start.minute,
				self.config_nighttime_start.second
			)
			async_track_time_change(
				self.hass,
				self._async_timer_trigger,
				self.config_nighttime_end.hour,
				self.config_nighttime_end.minute,
				self.config_nighttime_end.second
			)
		
		@callback
		def _async_startup(*_):
			"""Init on startup."""
			
			_LOGGER.info("better_thermostat %s: Starting version %s. Waiting for entity to be ready...", self.name, self.version)
			
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
			if self._current_temperature_setpoint is None:
				# If we have a previously saved temperature
				if old_state.attributes.get(ATTR_TEMPERATURE) is None:
					self._current_temperature_setpoint = self.min_temp
					_LOGGER.debug(
						"better_thermostat %s: Undefined target temperature, falling back to %s",
						self.name,
						self._current_temperature_setpoint
					)
				else:
					self._current_temperature_setpoint = float(old_state.attributes[ATTR_TEMPERATURE])
			if not self._hvac_mode and old_state.state:
				self._hvac_mode = old_state.state
			if not old_state.attributes.get(ATTR_STATE_LAST_CHANGE):
				self.last_change = old_state.attributes.get(ATTR_STATE_LAST_CHANGE)
			else:
				self.last_change = HVAC_MODE_OFF
			if not old_state.attributes.get(ATTR_STATE_WINDOW_OPEN):
				self.window_open = old_state.attributes.get(ATTR_STATE_WINDOW_OPEN)
			if not old_state.attributes.get(ATTR_STATE_DAY_SET_TEMP):
				self.last_daytime_setpoint = old_state.attributes.get(ATTR_STATE_DAY_SET_TEMP)
			if not old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT):
				self.call_for_heat = old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT)
			if not old_state.attributes.get(ATTR_STATE_NIGHT_MODE):
				self.night_mode_active = old_state.attributes.get(ATTR_STATE_NIGHT_MODE)
				if self.night_mode_active:
					if self.config_nighttime_setpoint and isinstance(self.config_nighttime_setpoint, numbers.Number):
						self._current_temperature_setpoint = float(self.config_nighttime_setpoint)
					else:
						_LOGGER.error(
							"better_thermostat %s: Night temp '%s' is not a number",
							self.name,
							str(self.config_nighttime_setpoint)
						)
		
		else:
			# No previous state, try and restore defaults
			if self._current_temperature_setpoint is None:
				_LOGGER.warning(
					"better_thermostat %s: No previously saved temperature found on startup, setting default value %s and turn heat off",
					self.name,
					self._current_temperature_setpoint
				)
				self._current_temperature_setpoint = self.min_temp
				self._hvac_mode = HVAC_MODE_OFF
		
		# if hvac mode could not be restored, turn heat off
		if not self._hvac_mode:
			_LOGGER.warning(
				"better_thermostat %s: No previously hvac mode found on startup, turn heat off",
				self.name
			)
			self._hvac_mode = HVAC_MODE_OFF
	
	async def startup(self):
		"""Run startup tasks."""
		window = None
		await asyncio.sleep(5)
		
		while self.startup_running:
			sensor_state = self.hass.states.get(self.config_room_air_temperature_sensor)
			trv_state = self.hass.states.get(self.config_heater_entity)
			
			if sensor_state is None:
				_LOGGER.error(
					"better_thermostat %s: configured temperature sensor with id '%s' could not be located",
					self.name,
					self.config_room_air_temperature_sensor
				)
				return False
			if trv_state is None:
				_LOGGER.error(
					"better_thermostat %s: configured TRV/climate entry with id '%s' could not be located",
					self.name,
					self.config_heater_entity
				)
				return False
			if self.config_window_sensors_group:
				window = self.hass.states.get(self.config_window_sensors_group)
				
				if window is None:
					_LOGGER.error(
						"better_thermostat %s: configured window sensor entry or group with id '%s' could not be located",
						self.name,
						self.config_window_sensors_group
					)
					return False
				
				# make sure window has a state variable
				try:
					if window.state:
						pass
				except (ValueError, NameError, AttributeError):
					_LOGGER.error(
						"better_thermostat %s: configured window sensor entry or group with id '%s' could not be located",
						self.name,
						self.config_window_sensors_group
					)
					return False
			
			_ready = True
			
			if sensor_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info(
					"better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
					self.name,
					self.config_room_air_temperature_sensor
				)
				_ready = False
			if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info(
					"better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
					self.name,
					self.config_heater_entity
				)
				_ready = False
			
			if self.hass.states.get(self.config_heater_entity).attributes.get('device') is None:
				_LOGGER.info(
					"better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
					self.name,
					self.config_heater_entity
				)
				_ready = False
			
			if self.config_window_sensors_group in (STATE_UNAVAILABLE, STATE_UNKNOWN, None) or window.state in (
					STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info(
					"better_thermostat %s: waiting for window sensor entity with id '%s' to become fully available...",
					self.name,
					self.config_window_sensors_group
				)
				_ready = False
			
			if not _ready:
				_LOGGER.info("better_thermostat %s: retry startup in 15 seconds...", self.name)
				await asyncio.sleep(15)
				continue
			
			if self.config_window_sensors_group:
				window = self.hass.states.get(self.config_window_sensors_group)
				
				check = window.state
				if check == 'on':
					self.window_open = True
					self._hvac_mode = HVAC_MODE_OFF
				else:
					self.window_open = False
					self.closed_window_triggered = False
				_LOGGER.debug(
					"better_thermostat %s: detected window state st startup: %s",
					self.name,
					"Open" if self.window_open else "Closed"
				)
			
			self.startup_running = False
			entity_registry = await self.hass.helpers.entity_registry.async_get_registry()
			reg_entity = entity_registry.async_get(self.config_heater_entity)
			entity_entries = async_entries_for_config_entry(entity_registry, reg_entity.config_entry_id)
			for entity in entity_entries:
				uid = entity.unique_id
				if "local_temperature_calibration" in uid:
					self.local_temperature_calibration_entity = entity.entity_id
				if "valve_position" in uid:
					self.valve_position_entity = entity.entity_id
			self._async_update_temp(sensor_state)
			self.async_write_ha_state()
			await asyncio.sleep(5)
			_LOGGER.info("better_thermostat %s: startup completed.", self.name)
			await self._async_control_heating()
		return True
	
	@property
	def extra_state_attributes(self):
		"""Return the device specific state attributes."""
		dev_specific = {
			ATTR_STATE_WINDOW_OPEN  : self.window_open,
			ATTR_STATE_NIGHT_MODE   : self.night_mode_active,
			ATTR_STATE_CALL_FOR_HEAT: self.call_for_heat,
			ATTR_STATE_LAST_CHANGE  : self.last_change,
			ATTR_STATE_DAY_SET_TEMP : self.last_daytime_setpoint, }
		
		return dev_specific
	
	@callback
	def _nighttime(self, current_time):
		"""
		Return whether it is nighttime.
		@param current_time: time.time()
		@return: bool True if it is nighttime; None if not configured
		"""
		_return_value = None
		
		# one or more of the inputs is None or empty
		if not all([self.config_nighttime_start, self.config_nighttime_end, current_time]):
			return _return_value
		
		# fetch to instance variables, since we might want to swap them
		start_time, end_time = self.config_nighttime_start, self.config_nighttime_end
		
		# if later set to true we'll swap the variables and output boolean, 'cause we use the logic backwards
		#   if the nighttime passes not over midnight, like (01:00 to 05:00) we use the inverted logic
		#   while something like 23:00 to 05:00 would use the default
		_reverse = False
		
		if start_time.hour < end_time.hour or (start_time.hour == end_time.hour and start_time.minute < end_time.minute):
			# not passing midnight, so we use the inverted logic
			_reverse = True
			start_time, end_time = end_time, start_time
		
		# if we are after the start time, but before the end time, we are in the night
		if (current_time.hour > start_time.hour or (
				current_time.hour == start_time.hour and current_time.minute >= start_time.minute)) and current_time.hour < end_time.hour or (
				current_time.hour == end_time.hour and current_time.minute < end_time.minute):
			_return_value = True
		
		# flip output, since we flipped the start/end time
		if _reverse:
			return not _return_value
		return _return_value
	
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
		if self._setpoint_step is not None:
			return self._setpoint_step
		
		return super().precision
	
	@property
	def temperature_unit(self):
		"""Return the unit of measurement."""
		return self._temperature_unit
	
	@property
	def current_temperature(self):
		"""Return the sensor temperature."""
		return self._current_room_temperature_sensor_value
	
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
			if self.hass.states.get(self.config_heater_entity).attributes.get('position') is not None:
				if check_float(self.hass.states.get(self.config_heater_entity).attributes.get('position')):
					valve = float(self.hass.states.get(self.config_heater_entity).attributes.get('position'))
					if valve > 0:
						return CURRENT_HVAC_HEAT
					else:
						return CURRENT_HVAC_IDLE
			
			if self.hass.states.get(self.config_heater_entity).attributes.get('pi_heating_demand') is not None:
				if check_float(self.hass.states.get(self.config_heater_entity).attributes.get('pi_heating_demand')):
					valve = float(self.hass.states.get(self.config_heater_entity).attributes.get('pi_heating_demand'))
					if valve > 0:
						return CURRENT_HVAC_HEAT
					else:
						return CURRENT_HVAC_IDLE
		except (RuntimeError, ValueError, AttributeError, KeyError, TypeError, NameError, IndexError) as e:
			_LOGGER.error("better_thermostat %s: RuntimeError occurred while running TRV operation, %s", self.name, e)
		
		if not self._is_device_active:
			return CURRENT_HVAC_IDLE
		return CURRENT_HVAC_HEAT
	
	@property
	def target_temperature(self):
		"""Return the temperature we try to reach."""
		return self._current_temperature_setpoint
	
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
			_LOGGER.error("better_thermostat %s: Unsupported hvac_mode %s", self.name, hvac_mode)
		self.async_write_ha_state()
		if self.closed_window_triggered or self.ignore_state_updates:
			return
		await self._async_control_heating()
	
	async def async_set_temperature(self, **kwargs):
		"""Set new target temperature."""
		temperature = kwargs.get(ATTR_TEMPERATURE)
		if temperature is None:
			return
		self._current_temperature_setpoint = temperature
		self.async_write_ha_state()
		if self.closed_window_triggered or self.ignore_state_updates:
			return
		await self._async_control_heating()
	
	@property
	def min_temp(self):
		"""Return the minimum temperature."""
		if self._minimal_set_temperature is not None:
			return self._minimal_set_temperature
		
		# get default temp from super class
		return super().min_temp
	
	@property
	def max_temp(self):
		"""Return the maximum temperature."""
		if self._maximal_set_temperature is not None:
			return self._maximal_set_temperature
		
		# Get default temp from super class
		return super().max_temp
	
	@callback
	async def _async_timer_trigger(self, current_time):
		"""
		Triggered by night mode timer.
		@param current_time:
		"""
		
		_is_night = self._nighttime(current_time)
		
		if _is_night is None:
			_LOGGER.error("better_thermostat %s: Error while checking if it is night", self.name)
			return
		elif _is_night:
			_LOGGER.debug("better_thermostat %s: Night mode activated", self.name)
			self.last_daytime_setpoint = self._current_temperature_setpoint
			self._current_temperature_setpoint = self.config_nighttime_setpoint
			self.night_mode_active = True
		
		else:
			_LOGGER.debug("ai_thermostat %s: Day mode activated", self.name)
			if self.last_daytime_setpoint is None:
				_LOGGER.error("better_thermostat %s: Could not load last daytime temp; continue using the current setpoint", self.name)
			else:
				self._current_temperature_setpoint = self.last_daytime_setpoint
			self.night_mode_active = False
		
		self.async_write_ha_state()
		await self._async_control_heating()
	
	@callback
	async def _async_window_changed(self):
		if self.startup_running:
			return
		if self.hass.states.get(self.config_heater_entity) is not None:
			await asyncio.sleep(int(self.config_window_delay))
			check = self.hass.states.get(self.config_window_sensors_group).state
			if check == 'on':
				self.window_open = True
			else:
				self.window_open = False
				self.closed_window_triggered = False
			_LOGGER.debug("better_thermostat %s: Window (group) state changed to %s", self.name, "open" if self.window_open else "closed")
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
		if self.closed_window_triggered or self.ignore_state_updates:
			return
		await self._async_control_heating()
	
	@callback
	def _async_update_temp(self, state):
		"""Update thermostat with the latest state from sensor."""
		try:
			self._current_room_temperature_sensor_value = float(state.state)
		except (ValueError, AttributeError, KeyError, TypeError, NameError, IndexError):
			_LOGGER.error(
				"better_thermostat %s: Unable to update temperature sensor status from status update, current temperature not a number",
				self.name
			)
	
	@callback
	async def _async_trv_changed(self, event):
		"""Handle temperature changes."""
		if self.startup_running:
			return
		
		old_state = event.data.get("old_state")
		new_state = event.data.get("new_state")
		
		if new_state is None or old_state is None:
			return
		
		# fetch device model from HA if necessary
		get_device_model(self)
		
		if new_state.attributes is not None:
			try:
				remapped_state = convert_inbound_states(self, new_state.attributes)
				
				# write valve position to local variable
				if remapped_state.get(ATTR_VALVE_POSITION) is not None:
					self._last_reported_valve_position = remapped_state.get(ATTR_VALVE_POSITION)
					self._last_reported_valve_position_update_wait_lock.release()
				
				if old_state.attributes.get('system_mode') != new_state.attributes.get('system_mode'):
					self._hvac_mode = remapped_state.get('system_mode')
					
					if self._hvac_mode != HVAC_MODE_OFF and self.window_open:
						self._hvac_mode = HVAC_MODE_OFF
						_LOGGER.debug("better_thermostat %s: Window open, turn off the heater", self.name)
						await self._async_control_heating()
				
				if not self.ignore_state_updates and new_state.attributes.get(
						'current_heating_setpoint'
				) is not None and self._hvac_mode != HVAC_MODE_OFF and self.calibration_type == 0:
					self._current_temperature_setpoint = float(new_state.attributes.get('current_heating_setpoint'))
			
			except TypeError:
				_LOGGER.debug("better_thermostat entity not ready or device is currently not supported")
			
			self.async_write_ha_state()
	
	async def trv_valve_maintenance(self):
		"""Maintenance of the TRV valve."""
		
		_LOGGER.info("better_thermostat %s: maintenance started", self.name)
		
		self.ignore_state_updates = True
		
		if self.model == "TS0601_thermostat":
			_LOGGER.debug("better_thermostat %s: maintenance will run TS0601_thermostat variant of cycle", self.name)
			
			# get current HVAC mode from HA
			try:
				_last_hvac_mode = self.hass.states.get(self.config_heater_entity).state
			except:
				_LOGGER.error("better_thermostat %s: Could not load current HVAC mode", self.name)
				self.ignore_state_updates = False
				return
			
			_i = 0
			_retry_limit_reached = False
			
			while True:
				# close valve
				_set_HVAC_mode_retry = 0
				while not self._last_reported_valve_position == 0:
					# send close valve command and wait for the valve to close
					await set_trv_values(self, 'system_mode', 'off')
					# wait for an update by the TRV on the valve position
					await self._last_reported_valve_position_update_wait_lock.acquire()
					if not self._last_reported_valve_position == 0 and _set_HVAC_mode_retry < 3:
						_set_HVAC_mode_retry += 1
						continue
					elif _set_HVAC_mode_retry == 3:
						_LOGGER.error("better_thermostat %s: maintenance could not close valve after 3 retries", self.name)
						_retry_limit_reached = True
						break
					# wait 60 seconds to not overheat the motor
					await asyncio.sleep(60)
				
				if _retry_limit_reached:
					_LOGGER.error("better_thermostat %s: maintenance was aborted prematurely due to errors", self.name)
					break
				
				# end loop after 3 opening cycles
				elif _i > 3:
					_LOGGER.info("better_thermostat %s: maintenance completed", self.name)
					break
				
				# open valve
				_set_HVAC_mode_retry = 0
				while not self._last_reported_valve_position == 100:
					# send open valve command and wait for the valve to open
					await self.hass.services.async_call(
						'climate', SERVICE_SET_HVAC_MODE, {'entity_id': self.config_heater_entity, 'hvac_mode': 'heat'}, blocking=True
					)
					await self._last_reported_valve_position_update_wait_lock.acquire()
					if not self._last_reported_valve_position == 0 and _set_HVAC_mode_retry < 3:
						_set_HVAC_mode_retry += 1
						continue
					elif _set_HVAC_mode_retry == 3:
						_LOGGER.error("better_thermostat %s: maintenance could not open valve after 3 retries", self.name)
						_retry_limit_reached = True
						break
					# wait 60 seconds to not overheat the motor
					await asyncio.sleep(60)
				
				if _retry_limit_reached:
					_LOGGER.error("better_thermostat %s: maintenance was aborted prematurely due to errors", self.name)
					break
				
				_i += 1
			
			# returning the TRV to the previous HVAC mode
			await self.hass.services.async_call(
				'climate', SERVICE_SET_HVAC_MODE, {'entity_id': self.config_heater_entity, 'hvac_mode': _last_hvac_mode}, blocking=True
			)
			# give the TRV time to process the mode change and report back to HA
			await asyncio.sleep(120)
		
		else:
			
			valve_position_available = False
			# check if there's a valve_position field
			try:
				self.hass.states.get(self.config_heater_entity).attributes.get('valve_position')
				valve_position_available = True
			except:
				pass
			
			if valve_position_available:
				await set_trv_values(self, 'valve_position', 255)
				await asyncio.sleep(60)
				await set_trv_values(self, 'valve_position', 0)
				await asyncio.sleep(60)
				await set_trv_values(self, 'valve_position', 255)
				await asyncio.sleep(60)
				await set_trv_values(self, 'valve_position', 0)
				await asyncio.sleep(60)
			else:
				await set_trv_values(self, 'temperature', 30)
				await asyncio.sleep(60)
				await set_trv_values(self, 'temperature', 5)
				await asyncio.sleep(60)
				await set_trv_values(self, 'temperature', 30)
				await asyncio.sleep(60)
				await set_trv_values(self, 'temperature', 5)
				await asyncio.sleep(60)
		
		self.ignore_state_updates = False
		
		# restarting normal heating control immediately
		await self._async_control_heating()
	
	async def _async_control_heating(self):
		"""main heating control function"""
		if self.ignore_state_updates or self.startup_running:
			return
		async with self._heating_control_lock:
			if all(
					[self._current_room_temperature_sensor_value, self._current_temperature_setpoint, self._hvac_mode,
					 self.hass.states.get(self.config_heater_entity).attributes]
			) and not self.startup_running:
				self.ignore_state_updates = True
				# Use the same precision and min and max as the TRV
				if self.hass.states.get(self.config_heater_entity).attributes.get('target_temp_step') is not None:
					self._setpoint_step = float(self.hass.states.get(self.config_heater_entity).attributes.get('target_temp_step'))
				else:
					self._setpoint_step = 1
				if self.hass.states.get(self.config_heater_entity).attributes.get('min_temp') is not None:
					self._minimal_set_temperature = float(self.hass.states.get(self.config_heater_entity).attributes.get('min_temp'))
				else:
					self._minimal_set_temperature = 5
				if self.hass.states.get(self.config_heater_entity).attributes.get('max_temp') is not None:
					self._maximal_set_temperature = float(self.hass.states.get(self.config_heater_entity).attributes.get('max_temp'))
				else:
					self._maximal_set_temperature = 30
				
				# check weather predictions or ambient air temperature if available
				if self.config_weather_sensor is not None:
					self.call_for_heat = self.check_weather_prediction()
				elif self.config_outdoor_air_temperature_sensor is not None:
					self.call_for_heat = self.check_ambient_air_temperature()
				else:
					self.call_for_heat = True
				
				if self.call_for_heat is None:
					_LOGGER.warning(
						"better_thermostat: call for heat decision: could not evaluate sensor/weather entity data, force heat on"
					)
					self.call_for_heat = True
				
				# window open detection and weather detection force turn TRV off
				if self.window_open and not self.closed_window_triggered:
					self.last_change = self._hvac_mode
					self._hvac_mode = HVAC_MODE_OFF
					self.closed_window_triggered = True
				elif not self.window_open and self.closed_window_triggered:
					self._hvac_mode = self.last_change
				
				# check if's summer
				if self._hvac_mode != HVAC_MODE_OFF and not self.window_open and not self.call_for_heat and not self.load_saved_state:
					self.last_change = self._hvac_mode
					self._hvac_mode = HVAC_MODE_OFF
					self.load_saved_state = True
				elif self.load_saved_state and self.call_for_heat and not self.window_open:
					self._hvac_mode = self.last_change
					self.load_saved_state = False
				
				try:
					remapped_states = convert_outbound_states(self, self._hvac_mode)
					converted_hvac_mode = remapped_states.get('system_mode')
					current_heating_setpoint = self._current_temperature_setpoint
					has_real_mode = True if self.hass.states.get(self.config_heater_entity).attributes.get(
						'system_mode'
					) is not None else False
					calibration = remapped_states.get('local_temperature_calibration')
					
					# if off do nothing
					if self.closed_window_triggered or self._hvac_mode == HVAC_MODE_OFF:
						if has_real_mode:
							if self.hass.states.get(self.config_heater_entity).attributes.get('system_mode') == HVAC_MODE_OFF:
								self.ignore_state_updates = False
								return
						if self.calibration_type == 1:
							if float(self.hass.states.get(self.config_heater_entity).attributes.get('current_heating_setpoint')) <= 5.0:
								self.ignore_state_updates = False
								return
					
					do_calibration = False
					if self.last_calibration_timestamp is None:
						do_calibration = True
					elif datetime.now() > (self.last_calibration_timestamp + timedelta(seconds=20)):
						do_calibration = True
					
					if do_calibration:
						_LOGGER.debug("better_thermostat: running calibration")
						self.last_calibration_timestamp = datetime.now()
					
					_LOGGER.debug(
						"better_thermostat triggered states > window open: %s night mode: %s Mode: %s set: %s has_mode: %s calibration: %s "
						"set_temp: %s cur_temp: %s Model: %s calibration type: %s call for heat: %s TRV: %s",
						self.window_open,
						self.night_mode_active,
						converted_hvac_mode,
						self._hvac_mode,
						has_real_mode,
						calibration,
						current_heating_setpoint,
						self._current_room_temperature_sensor_value,
						self.model,
						self.calibration_type,
						self.call_for_heat,
						self.hass.states.get(self.config_heater_entity).attributes.get('device').get('friendlyName')
					)
					
					# Using on temperature based calibration, don't update the temp if it's the same
					if self.calibration_type == 1 and float(
							self.hass.states.get(self.config_heater_entity).attributes.get('current_heating_setpoint')
					) != float(calibration):
						await set_trv_values(self, 'temperature', float(calibration))
						
						# Using on local calibration, don't update the temp if its off, some TRV changed to 5°C when
						# off after a while, don't update the temp
						if self.calibration_type == 0 and not self.window_open and converted_hvac_mode != HVAC_MODE_OFF and float(
								current_heating_setpoint
						) != 5.0 and self.call_for_heat:
							await set_trv_values(self, 'temperature', float(current_heating_setpoint))
						
						# Using on local calibration, update only if the TRV is not in window open mode
						if self.calibration_type == 0 and not self.window_open and do_calibration:
							await set_trv_values(self, 'local_temperature_calibration', calibration)
					
					# Only set the system mode if the TRV has this option
					if has_real_mode:
						await set_trv_values(self, 'system_mode', converted_hvac_mode)
					
					self.ignore_state_updates = False
					
					# Check if a valve_maintenance is needed
					if self.conf_valve_maintenance:
						current_time = datetime.now()
						if current_time > self.next_valve_maintenance:
							_LOGGER.debug("better_thermostat: valve_maintenance triggerd")
							await self.trv_valve_maintenance()
							self.next_valve_maintenance = datetime.now() + timedelta(days=5)
				
				except TypeError:
					_LOGGER.debug("better_thermostat entity not ready or device is currently not supported")
					self.ignore_state_updates = False
	
	def check_weather_prediction(self):
		"""
		Checks configured weather entity for next two days of temperature predictions.
		@return: True if the maximum forcast temperature is lower than the off temperature; None if not successful
		"""
		if self.config_weather_sensor is None:
			_LOGGER.warning("better_thermostat: weather entity not available.")
			return None
		
		if self.conf_turnoff_temperature_threshold is None or not isinstance(self.conf_turnoff_temperature_threshold, float):
			_LOGGER.warning("better_thermostat: off_temperature not set or not a float.")
			return None
		
		try:
			forcast = self.hass.states.get(self.config_weather_sensor).attributes.get('forecast')
			if len(forcast) > 0:
				max_forcast_temp = math.ceil((float(forcast[0]['temperature']) + float(forcast[1]['temperature'])) / 2)
				_LOGGER.debug("better_thermostat: avg weather temp: %s", max_forcast_temp)
				return float(max_forcast_temp) < float(self.conf_turnoff_temperature_threshold)
			else:
				raise TypeError
		except TypeError:
			_LOGGER.warning("better_thermostat: no weather entity data found.")
			return None
	
	def check_ambient_air_temperature(self):
		"""
		Gets the history for two days and evaluates the necessary for heating.
		@return: returns True if the average temperature is lower than the off temperature; None if not successful
		"""
		if self.config_outdoor_air_temperature_sensor is None:
			return None
		
		if self.conf_turnoff_temperature_threshold is None or not isinstance(self.conf_turnoff_temperature_threshold, float):
			_LOGGER.warning("better_thermostat: off_temperature not set or not a float.")
			return None
		
		try:
			last_two_days_date_time = datetime.now() - timedelta(days=2)
			start = dt_util.as_utc(last_two_days_date_time)
			history_list = history.state_changes_during_period(
				self.hass, start, dt_util.as_utc(datetime.now()), self.config_outdoor_air_temperature_sensor
			)
			historic_sensor_data = history_list.get(self.config_outdoor_air_temperature_sensor)
		except TypeError:
			_LOGGER.warning("better_thermostat: no outdoor sensor data found.")
			return None
		
		# create a list from valid data in historic_sensor_data
		valid_historic_sensor_data = []
		for measurement in historic_sensor_data:
			if measurement.state is not None:
				try:
					valid_historic_sensor_data.append(float(measurement.state))
				except ValueError:
					pass
				except TypeError:
					pass
		
		# remove the upper and lower 5% of the data
		valid_historic_sensor_data.sort()
		valid_historic_sensor_data = valid_historic_sensor_data[
		                             int(len(valid_historic_sensor_data) * 0.05):int(len(valid_historic_sensor_data) * 0.95)]
		
		if len(valid_historic_sensor_data) == 0:
			_LOGGER.warning("better_thermostat: no valid outdoor sensor data found.")
			return None
		
		# calculate the average temperature
		avg_temp = math.ceil(sum(valid_historic_sensor_data) / len(valid_historic_sensor_data))
		_LOGGER.debug("better_thermostat: avg outdoor temp: %s", avg_temp)
		return float(avg_temp) < float(self.conf_turnoff_temperature_threshold)
	
	@property
	def _is_device_active(self):
		state_off = self.hass.states.is_state(self.config_heater_entity, "off")
		state_heat = self.hass.states.is_state(self.config_heater_entity, "heat")
		state_auto = self.hass.states.is_state(self.config_heater_entity, "auto")
		
		if not self.hass.states.get(self.config_heater_entity):
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
