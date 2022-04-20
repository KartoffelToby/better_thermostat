"""Better Thermostat"""

import asyncio
import logging
from abc import ABC
from datetime import datetime, timedelta
from random import randint

import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol
from homeassistant.components.climate import ClimateEntity, PLATFORM_SCHEMA
from homeassistant.components.climate.const import CURRENT_HVAC_HEAT, CURRENT_HVAC_IDLE, CURRENT_HVAC_OFF, HVAC_MODE_HEAT, HVAC_MODE_OFF
from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID, EVENT_HOMEASSISTANT_START
from homeassistant.core import callback, CoreState
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity

from . import DOMAIN, PLATFORMS
from .const import (
	ATTR_STATE_CALL_FOR_HEAT, ATTR_STATE_DAY_SET_TEMP, ATTR_STATE_LAST_CHANGE, ATTR_STATE_NIGHT_MODE, ATTR_STATE_WINDOW_OPEN, CONF_HEATER,
	CONF_MAX_TEMP, CONF_MIN_TEMP, CONF_NIGHT_END, CONF_NIGHT_START, CONF_NIGHT_TEMP, CONF_OFF_TEMPERATURE, CONF_OUTDOOR_SENSOR,
	CONF_PRECISION, CONF_SENSOR, CONF_SENSOR_WINDOW, CONF_TARGET_TEMP, CONF_VALVE_MAINTENANCE, CONF_WEATHER, CONF_WINDOW_TIMEOUT,
	DEFAULT_NAME, SUPPORT_FLAGS, VERSION
)
from .controlling import control_queue, set_hvac_mode, set_target_temperature
from .events.temperature import trigger_temperature_change
from .events.time import trigger_time
from .events.trv import trigger_trv_change
from .events.window import trigger_window_change, window_queue
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
	"""Set up the Better Thermostat platform.

	Parameters
	----------
	hass : 
		Home Assistant object.
	config : 
		Configuration object from Platform.
	async_add_entities :
		Callback to add entities. 
	discovery_info :
		Needed for bridge like devices. 
	
	Returns
	-------
	None
	"""
	await async_setup_reload_service(hass, DOMAIN, PLATFORMS)
	
	async_add_entities(
		[
			BetterThermostat(
				config.get(CONF_NAME),
				config.get(CONF_HEATER),
				config.get(CONF_SENSOR),
				config.get(CONF_SENSOR_WINDOW),
				config.get(CONF_WINDOW_TIMEOUT),
				config.get(CONF_WEATHER),
				config.get(CONF_OUTDOOR_SENSOR),
				config.get(CONF_OFF_TEMPERATURE),
				config.get(CONF_VALVE_MAINTENANCE),
				config.get(CONF_NIGHT_TEMP),
				config.get(CONF_NIGHT_START),
				config.get(CONF_NIGHT_END),
				config.get(CONF_MIN_TEMP),
				config.get(CONF_MAX_TEMP),
				config.get(CONF_TARGET_TEMP),
				config.get(CONF_PRECISION),
				hass.config.units.temperature_unit,
				config.get(CONF_UNIQUE_ID),
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
		window_id,
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
		"""Initialize the thermostat.

		Parameters
		----------
		name : 
		heater_entity_id : 
		sensor_entity_id : 
		window_id : 
		window_delay : 
		weather_entity : 
		outdoor_sensor : 
		off_temperature : 
		valve_maintenance : 
		night_temp : 
		night_start : 
		night_end : 
		min_temp : 
		max_temp : 
		target_temp : 
		precision : 
		unit : 
		unique_id : 
		device_class : 
		state_class : 
		"""
		self._name = name
		self.heater_entity_id = heater_entity_id
		self.sensor_entity_id = sensor_entity_id
		self.window_id = window_id
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
		self.calibration_type = 0
		self.last_daytime_temp = None
		self.closed_window_triggered = False
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
		self._last_window_state = None
		self._temp_lock = asyncio.Lock()
		self._last_reported_valve_position = None
		self._last_reported_valve_position_update_wait_lock = asyncio.Lock()
		self.control_queue_task = asyncio.Queue()
		self.window_queue_task = asyncio.Queue()
		asyncio.create_task(control_queue(self))
		asyncio.create_task(window_queue(self))

	async def async_added_to_hass(self):
		"""Run when entity about to be added.

		Returns
		-------
		None
		"""
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
		if self.window_id:
			async_track_state_change_event(
				self.hass, [self.window_id], self._trigger_window_change
			)
		
		# check if night mode was configured
		if None not in (self.night_start, self.night_end, self.night_temp):
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
			"""Init on startup.

			Parameters
			----------
			_ : 
				All parameters are piped.
			"""
			_LOGGER.info("better_thermostat %s: Starting version %s. Waiting for entity to be ready...", self.name, self.version)
			
			loop = asyncio.get_event_loop()
			loop.create_task(startup(self))
		
		if self.hass.state == CoreState.running:
			_async_startup()
		else:
			self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)
	
	async def _trigger_time(self, event):
		await trigger_time(self, event)
	
	async def _trigger_temperature_change(self, event):
		await trigger_temperature_change(self, event)
	
	async def _trigger_trv_change(self, event):
		await trigger_trv_change(self, event)
	
	async def _trigger_window_change(self, event):
		await trigger_window_change(self, event)
	
	@property
	def extra_state_attributes(self):
		"""Return the device specific state attributes.

		Returns
		-------
		dict
			Attribute dictionary for the extra device specific state attributes.
		"""
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
		"""Return if thermostat is available.

		Returns
		-------
		bool
			True if the thermostat is available.
		"""
		return not self.startup_running
	
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
		"""Return the current HVAC action
		"""
		
		if self._bt_hvac_mode == HVAC_MODE_OFF:
			_LOGGER.debug(f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_OFF}")
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
			_LOGGER.debug(f"better_thermostat {self.name}: HA asked for our HVAC action, we will respond with: {CURRENT_HVAC_HEAT}")
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
