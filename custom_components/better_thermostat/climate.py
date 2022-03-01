"""Better Thermostat"""

import asyncio
import logging
import numbers
from abc import ABC
from datetime import datetime, timedelta
from random import randint

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.climate import ClimateEntity, PLATFORM_SCHEMA
from homeassistant.components.climate.const import (CURRENT_HVAC_HEAT, CURRENT_HVAC_IDLE, CURRENT_HVAC_OFF, HVAC_MODE_HEAT, HVAC_MODE_OFF)
from homeassistant.const import (ATTR_TEMPERATURE, CONF_NAME, CONF_UNIQUE_ID, EVENT_HOMEASSISTANT_START)
from homeassistant.core import callback, CoreState
from homeassistant.helpers.event import (async_track_state_change_event, async_track_time_change)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN, PLATFORMS
from .const import (
	ATTR_STATE_CALL_FOR_HEAT, ATTR_STATE_DAY_SET_TEMP, ATTR_STATE_LAST_CHANGE, ATTR_STATE_NIGHT_MODE, ATTR_STATE_WINDOW_OPEN, CONF_HEATER,
	CONF_MAX_TEMP, CONF_MIN_TEMP, CONF_NIGHT_END, CONF_NIGHT_START, CONF_NIGHT_TEMP, CONF_OFF_TEMPERATURE, CONF_OUTDOOR_SENSOR,
	CONF_PRECISION, CONF_SENSOR, CONF_SENSOR_WINDOW, CONF_TARGET_TEMP, CONF_VALVE_MAINTENANCE, CONF_WEATHER, CONF_WINDOW_TIMEOUT,
	DEFAULT_NAME, SUPPORT_FLAGS, VERSION
)
from .controlling import set_hvac_mode, set_target_temperature
from .events.temperature import trigger_temperature_change
from .events.time import trigger_time
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change
from .helpers import startup
from .models.models import get_device_model, load_device_config

_LOGGER = logging.getLogger(__name__)

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
		vol.Optional(CONF_UNIQUE_ID)                       : cv.string,
		vol.Optional(CONF_MIN_TEMP, default=5.0)           : vol.Coerce(float),
		vol.Optional(CONF_MAX_TEMP, default=35.0)          : vol.Coerce(float),
		vol.Optional(CONF_PRECISION, default=0.5)          : vol.Coerce(float),
	}
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
	"""Set up the Better Thermostat platform."""
	
	await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
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
	
	min_temp = config.get(CONF_MIN_TEMP)
	max_temp = config.get(CONF_MAX_TEMP)
	target_temp = config.get(CONF_TARGET_TEMP)
	precision = config.get(CONF_PRECISION)
	unit = hass.config.units.temperature_unit
	unique_id = config.get(CONF_UNIQUE_ID)
	
	async_add_entities(
		[
			BetterThermostat(
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


class BetterThermostat(ClimateEntity, RestoreEntity, ABC):
	"""Representation of a Better Thermostat device."""
	
	def __init__(
			self,
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
		self._name = name
		self.heater_entity_id = heater_entity_id
		self.sensor_entity_id = sensor_entity_id
		self.window_sensors_entity_ids = window_sensors_entity_ids
		self.window_delay = window_delay or 0
		self.weather_entity = weather_entity
		self.outdoor_sensor = outdoor_sensor
		self.off_temperature = off_temperature or None
		self.valve_maintenance = valve_maintenance
		self.night_temp = night_temp or None
		self.night_start = dt_util.parse_time(night_start) or None
		self.night_end = dt_util.parse_time(night_end) or None
		self._trv_hvac_mode = None
		self._bt_hvac_mode = None
		self._saved_target_temp = target_temp or None
		self._target_temp_step = precision
		self._TRV_target_temp_step = 0.5
		self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
		self._cur_temp = None
		self._temp_lock = asyncio.Lock()
		self._min_temp = min_temp
		self._TRV_min_temp = 5.0
		self._max_temp = max_temp
		self._TRV_max_temp = 30.0
		self._target_temp = target_temp
		self._unit = unit
		self._unique_id = unique_id
		self._support_flags = SUPPORT_FLAGS
		self.window_open = None
		self._is_away = False
		self.startup_running = True
		self.model = None
		self.next_valve_maintenance = datetime.now() + timedelta(hours=randint(1, 24 * 5))
		self.calibration_type = 2
		self.last_daytime_temp = None
		self.night_mode_active = None
		self.call_for_heat = None
		self.ignore_states = False
		self.last_calibration = None
		self.last_dampening_timestamp = None
		self._device_class = device_class
		self._state_class = state_class
		self.local_temperature_calibration_entity = None
		self.valve_position_entity = None
		self.version = VERSION
		self.last_change = None
		self.load_saved_state = False
		self._last_reported_valve_position = None
		self._last_reported_valve_position_update_wait_lock = asyncio.Lock()
	
	async def async_added_to_hass(self):
		"""Run when entity about to be added."""
		await super().async_added_to_hass()
		
		# fetch device model from HA if necessary
		self.model = await get_device_model(self)
	
		if self.model is None:
			_LOGGER.error("better_thermostat %s: can't read the device model of TVR. please check if you have a device in HA", self.name)
			return
		else:
			load_device_config(self)
			
		# Add listener
		async_track_state_change_event(
			self.hass, [self.sensor_entity_id], self._trigger_temperature_change
		)
		async_track_state_change_event(
			self.hass, [self.heater_entity_id], self._trigger_trv_change
		)
		if self.window_sensors_entity_ids:
			async_track_state_change_event(
				self.hass, [self.window_sensors_entity_ids], self._trigger_window_change
			)
		
		# check if night mode was configured
		if all([self.night_start, self.night_end, self.night_temp]):
			_LOGGER.debug("Night mode configured")
			async_track_time_change(
				self.hass,
				self._trigger_time,
				self.night_start.hour,
				self.night_start.minute,
				self.night_start.second,
			)
			async_track_time_change(
				self.hass,
				self._trigger_time,
				self.night_end.hour,
				self.night_end.minute,
				self.night_end.second,
			)
		
		@callback
		def _async_startup(*_):
			"""Init on startup."""
			
			_LOGGER.info("better_thermostat %s: Starting version %s. Waiting for entity to be ready...", self.name, self.version)
			
			loop = asyncio.get_event_loop()
			loop.create_task(startup(self))
		
		if self.hass.state == CoreState.running:
			_async_startup()
		else:
			self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)
		
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
						self._target_temp = self._min_temp
					# if the saved temperature is higher than the _max_temp, set it to _max_temp
					elif _old_target_temperature > self._max_temp:
						_LOGGER.warning(
							"better_thermostat %s: Saved target temperature %s is higher than _max_temp %s, setting to _max_temp",
							self.name,
							_old_target_temperature,
							self._min_temp
						)
						self._target_temp = self._max_temp
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
		self.async_write_ha_state()

	async def _trigger_time(self,event):
		await trigger_time(self,event)

	async def _trigger_temperature_change(self,event):
		await trigger_temperature_change(self,event)

	async def _trigger_trv_change(self,event):
		await trigger_trv_change(self,event)

	async def _trigger_window_change(self,event):
		await trigger_window_change(self)
	
	@property
	def extra_state_attributes(self):
		"""Return the device specific state attributes."""
		dev_specific = {
			ATTR_STATE_WINDOW_OPEN  : self.window_open,
			ATTR_STATE_NIGHT_MODE   : self.night_mode_active,
			ATTR_STATE_CALL_FOR_HEAT: self.call_for_heat,
			ATTR_STATE_LAST_CHANGE  : self.last_change,
			ATTR_STATE_DAY_SET_TEMP : self.last_daytime_temp,
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
		return self._bt_hvac_mode
	
	@property
	def hvac_action(self):
		"""Return the current HVAC action
		"""
		
		if self._bt_hvac_mode == HVAC_MODE_OFF:
			_LOGGER.debug(f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_OFF}")
			return CURRENT_HVAC_OFF
		if self._bt_hvac_mode == HVAC_MODE_HEAT:
			if self.window_open or not self.call_for_heat:
				_LOGGER.debug(
					f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_IDLE}. Window open: {self.window_open}, call for heat: {self.call_for_heat}"
					)
				return CURRENT_HVAC_IDLE
			_LOGGER.debug(f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_HEAT}")
			return CURRENT_HVAC_HEAT
	
	@property
	def target_temperature(self):
		"""Return the temperature we try to reach."""
		if not all([self._max_temp, self._min_temp, self._target_temp]):
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
		"""List of available operation modes."""
		return self._hvac_list
	
	async def async_set_hvac_mode(self, hvac_mode: str) -> None:
		"""Set hvac mode."""
		await set_hvac_mode(self, hvac_mode)
	
	async def async_set_temperature(self, **kwargs) -> None:
		"""Set new target temperature."""
		await set_target_temperature(self, **kwargs)
	
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
	
	@property
	def supported_features(self):
		"""Return the list of supported features."""
		return self._support_flags
