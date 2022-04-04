"""Helper functions for the Better Thermostat component."""

import asyncio
import logging
import numbers
from datetime import datetime

from homeassistant.components.climate.const import HVAC_MODE_OFF
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.entity_registry import async_entries_for_config_entry

from .const import ATTR_STATE_CALL_FOR_HEAT, ATTR_STATE_DAY_SET_TEMP, ATTR_STATE_LAST_CHANGE, ATTR_STATE_NIGHT_MODE, ATTR_STATE_WINDOW_OPEN
from .controlling import control_trv
from .models.utils import convert_to_float

_LOGGER = logging.getLogger(__name__)


def log_info(self, message):
	"""Log a message to the info log.

	Parameters
	----------
	self : 
		self instance of better_thermostat
	message : 
		the message to log 

	Returns
	-------
	None
	"""
	_LOGGER.debug(
		"better_thermostat with config name: %s, %s TRV: %s",
		self.name,
		message,
		self.hass.states.get(self.heater_entity_id).attributes.get('device').get('friendlyName')
	)


async def startup(self):
	"""Run startup tasks.

	Parameters
	----------
	self : 
		self instance of better_thermostat

	Returns
	-------
	None
	"""
	window = None
	await asyncio.sleep(5)
	
	while self.startup_running:
		sensor_state = self.hass.states.get(self.sensor_entity_id)
		trv_state = self.hass.states.get(self.heater_entity_id)
		
		if sensor_state is None:
			_LOGGER.error(
				"better_thermostat %s: configured temperature sensor with id '%s' could not be located",
				self.name,
				self.sensor_entity_id
			)
			return False
		if trv_state is None:
			_LOGGER.error(
				"better_thermostat %s: configured TRV/climate entry with id '%s' could not be located",
				self.name,
				self.heater_entity_id
			)
			return False
		if self.window_id:
			window = self.hass.states.get(self.window_id)
			
			if window is None:
				_LOGGER.error(
					"better_thermostat %s: configured window sensor entry or group with id '%s' could not be located",
					self.name,
					self.window_id
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
					self.window_id
				)
				return False
		
		_ready = True
		
		if sensor_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
			_LOGGER.info(
				"better_thermostat %s: waiting for sensor entity with id '%s' to become fully available...",
				self.name,
				self.sensor_entity_id
			)
			_ready = False
		if trv_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
			_LOGGER.info(
				"better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
				self.name,
				self.heater_entity_id
			)
			_ready = False
		if self.window_id:
			if self.window_id in (STATE_UNAVAILABLE, STATE_UNKNOWN, None) or window.state in (
				STATE_UNAVAILABLE, STATE_UNKNOWN, None):
				_LOGGER.info(
					"better_thermostat %s: waiting for window sensor entity with id '%s' to become fully available...",
					self.name,
					self.window_id
				)
				_ready = False
		
		if not _ready:
			_LOGGER.info("better_thermostat %s: retry startup in 15 seconds...", self.name)
			await asyncio.sleep(15)
			continue
		
		if self.window_id:
			window = self.hass.states.get(self.window_id)
			
			check = window.state
			if check == 'on':
				self.window_open = True
			else:
				self.window_open = False
			_LOGGER.debug(
				"better_thermostat %s: detected window state at startup: %s",
				self.name,
				"Open" if self.window_open else "Closed"
			)
		if not self.window_id:
			self.window_open = False
		
		self.startup_running = False
		entity_registry = await self.hass.helpers.entity_registry.async_get_registry()
		reg_entity = entity_registry.async_get(self.heater_entity_id)
		entity_entries = async_entries_for_config_entry(entity_registry, reg_entity.config_entry_id)
		for entity in entity_entries:
			uid = entity.unique_id
			# Make sure we use the correct device entities
			if entity.device_id == reg_entity.device_id:
				if "local_temperature_calibration" in uid:
					self.local_temperature_calibration_entity = entity.entity_id
				if "valve_position" in uid:
					self.valve_position_entity = entity.entity_id
		await asyncio.sleep(5)
		# Use the same precision and min and max as the TRV
		if self.hass.states.get(self.heater_entity_id).attributes.get('target_temp_step') is not None:
			self._TRV_target_temp_step = float(self.hass.states.get(self.heater_entity_id).attributes.get('target_temp_step'))
		else:
			self._TRV_target_temp_step = 1
		if self.hass.states.get(self.heater_entity_id).attributes.get('min_temp') is not None:
			self._TRV_min_temp = float(self.hass.states.get(self.heater_entity_id).attributes.get('min_temp'))
		else:
			self._TRV_min_temp = 5
		if self.hass.states.get(self.heater_entity_id).attributes.get('max_temp') is not None:
			self._TRV_max_temp = float(self.hass.states.get(self.heater_entity_id).attributes.get('max_temp'))
		else:
			self._TRV_max_temp = 30

		# Check If we have an old state
		if (old_state := await self.async_get_last_state()) is not None:
			# If we have no initial temperature, restore
			if self._target_temp is None:
				# If we have a previously saved temperature
				if old_state.attributes.get(ATTR_TEMPERATURE) is None:
					self._target_temp = self._min_temp
					_LOGGER.debug(
						"better_thermostat %s: Undefined target temperature, falling back to %s", self.name, self._target_temp
					)
				else:
					_old_target_temperature = float(old_state.attributes.get(ATTR_TEMPERATURE))
					# if the saved temperature is lower than the _min_temp, set it to _min_temp
					if _old_target_temperature < self._min_temp:
						_LOGGER.warning(
							"better_thermostat %s: Saved target temperature %s is lower than _min_temp %s, setting to _min_temp",
							self.name,
							_old_target_temperature,
							self._min_temp
						)
						_old_target_temperature = self._min_temp
					# if the saved temperature is higher than the _max_temp, set it to _max_temp
					elif _old_target_temperature > self._max_temp:
						_LOGGER.warning(
							"better_thermostat %s: Saved target temperature %s is higher than _max_temp %s, setting to _max_temp",
							self.name,
							_old_target_temperature,
							self._min_temp
						)
						_old_target_temperature = self._max_temp
					self._target_temp = _old_target_temperature
			if not self._bt_hvac_mode and old_state.state:
				self._bt_hvac_mode = old_state.state
			if not old_state.attributes.get(ATTR_STATE_LAST_CHANGE):
				self.last_change = old_state.attributes.get(ATTR_STATE_LAST_CHANGE)
			else:
				self.last_change = HVAC_MODE_OFF
			if not old_state.attributes.get(ATTR_STATE_WINDOW_OPEN):
				self.window_open = old_state.attributes.get(ATTR_STATE_WINDOW_OPEN)
			if not old_state.attributes.get(ATTR_STATE_DAY_SET_TEMP):
				self.last_daytime_temp = old_state.attributes.get(ATTR_STATE_DAY_SET_TEMP)
			if not old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT):
				self.call_for_heat = old_state.attributes.get(ATTR_STATE_CALL_FOR_HEAT)
			if not old_state.attributes.get(ATTR_STATE_NIGHT_MODE):
				self.night_mode_active = old_state.attributes.get(ATTR_STATE_NIGHT_MODE)
				if self.night_mode_active:
					if self.night_temp and isinstance(self.night_temp, numbers.Number):
						_night_temp = float(self.night_temp)
						# if the night temperature is lower than _min_temp, set it to _min_temp
						if _night_temp < self._min_temp:
							_LOGGER.error(
								"better_thermostat %s: Night temperature %s is lower than _min_temp %s, setting to _min_temp",
								self.name,
								_night_temp,
								self._min_temp
							)
							self._target_temp = self._min_temp
						# if the night temperature is higher than the _max_temp, set it to max_temp
						elif _night_temp > self._max_temp:
							_LOGGER.warning(
								"better_thermostat %s: Night temperature %s is higher than _max_temp %s, setting to _max_temp",
								self.name,
								_night_temp,
								self._min_temp
							)
							self._target_temp = self._max_temp
					else:
						_LOGGER.error("better_thermostat %s: Night temp '%s' is not a number", self.name, str(self.night_temp))
		
		else:
			# No previous state, try and restore defaults
			if self._target_temp is None:
				_LOGGER.info(
					"better_thermostat %s: No previously saved temperature found on startup, turning heat off",
					self.name
				)
				self._bt_hvac_mode = HVAC_MODE_OFF
		# if hvac mode could not be restored, turn heat off
		if not self._bt_hvac_mode:
			_LOGGER.warning(
				"better_thermostat %s: No previously hvac mode found on startup, turn heat off",
				self.name
			)
			self._bt_hvac_mode = HVAC_MODE_OFF
		self._last_window_state = self.window_open
		self._cur_temp = convert_to_float(str(sensor_state.state), self.name, "startup()")
		self.async_write_ha_state()
		_LOGGER.info("better_thermostat %s: startup completed.", self.name)
		await self.control_queue_task.put(self)
	return True


def check_float(potential_float):
	"""Check if a string is a float.

	Parameters
	----------
	potential_float : 
		the value to check

	Returns
	-------
	bool
		True if the value is a float, False otherwise.
		
	"""
	try:
		float(potential_float)
		return True
	except ValueError:
		return False


def convert_time(time_string):
	"""Convert a time string to a datetime object.

	Parameters
	----------
	time_string : 
		a string representing a time

	Returns
	-------
	datetime
		the converted time as a datetime object.
	None
		If the time string is not a valid time.
	"""
	try:
		_current_time = datetime.now()
		_get_hours_minutes = datetime.strptime(time_string, "%H:%M")
		return _current_time.replace(hour=_get_hours_minutes.hour, minute=_get_hours_minutes.minute, second=0, microsecond=0)
	except ValueError:
		return None
