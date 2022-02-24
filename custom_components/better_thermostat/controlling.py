import asyncio
import logging

from homeassistant.components.climate.const import HVAC_MODE_HEAT, HVAC_MODE_OFF, SERVICE_SET_HVAC_MODE, SERVICE_SET_TEMPERATURE
from homeassistant.components.number.const import (SERVICE_SET_VALUE)
from homeassistant.const import (ATTR_TEMPERATURE)

from .models.models import convert_outbound_states
from .models.utils import convert_to_float
from .weather import check_weather

_LOGGER = logging.getLogger(__name__)


async def control_trv(self):
	"""This is the main controller for the real TRV"""
	if self.startup_running:
		return
	async with self._temp_lock:
		self.ignore_states = True
		try:
			self.call_for_heat = await check_weather(self)
			_LOGGER.debug(f"better_thermostat {self.name}: Call for heat decision: {self.call_for_heat}")
			await check_window_state(self)
			await check_call_for_heat(self)
			if not self.call_for_heat:
				return await change_hvac_mode(self, HVAC_MODE_OFF)
			else:
				remapped_states = convert_outbound_states(self, self._hvac_mode)
				if remapped_states is None:
					return None
				
				converted_hvac_mode = remapped_states.get('system_mode') or None
				current_heating_setpoint = remapped_states.get('current_heating_setpoint') or None
				calibration = remapped_states.get('local_temperature_calibration') or None
				if converted_hvac_mode is not None:
					await change_hvac_mode(self, converted_hvac_mode)
				if current_heating_setpoint is not None:
					await change_target_temperature(self, current_heating_setpoint)
				if calibration is not None:
					await change_local_calibration(self, calibration)
		except ValueError as e:
			_LOGGER.error("better_thermostat %s: ValueError %s", self.name, e)
		self.ignore_states = False


async def check_window_state(self):
	# window open detection
	if self.window_open and not self.closed_window_triggered:
		self.last_change = self._hvac_mode
		self._hvac_mode = HVAC_MODE_OFF
		self.closed_window_triggered = True
	elif not self.window_open and self.closed_window_triggered:
		self._hvac_mode = self.last_change
		self.closed_window_triggered = False


async def check_call_for_heat(self):
	# check if there's a call for heat based on the weather
	if self._hvac_mode != HVAC_MODE_OFF and not self.window_open and not self.call_for_heat and not self.load_saved_state:
		self.last_change = self._hvac_mode
		self._hvac_mode = HVAC_MODE_OFF
		self.load_saved_state = True
	elif self.load_saved_state and self.call_for_heat and not self.window_open:
		self._hvac_mode = self.last_change
		self.load_saved_state = False


async def set_target_temperature(self, **kwargs):
	_new_setpoint = convert_to_float(kwargs.get(ATTR_TEMPERATURE), self.name, "controlling.set_target_temperature()")
	if _new_setpoint is None:
		_LOGGER.info(f"better_thermostat {self.name}: received a new setpoint from HA, but temperature attribute was not set, ignoring")
		return
	_LOGGER.info(f"better_thermostat {self.name}: received a new setpoint from HA: {_new_setpoint}")
	self._target_temp = _new_setpoint
	self.async_write_ha_state()
	await control_trv(self)


async def set_hvac_mode(self, hvac_mode):
	if hvac_mode in (HVAC_MODE_HEAT, HVAC_MODE_OFF):
		_LOGGER.debug(f"better_thermostat {self.name}: received new HVAC mode {hvac_mode}")
		self._hvac_mode = hvac_mode
	else:
		_LOGGER.error("better_thermostat %s: Unsupported hvac_mode %s", self.name, hvac_mode)
	self.async_write_ha_state()
	await control_trv(self)


async def change_hvac_mode(self, hvac_mode):
	await set_trv_values(self, 'system_mode', hvac_mode)


async def change_target_temperature(self, target_temp):
	# Using on local calibration, don't update the temp if its off, some TRV changed to 5 °C when off after a while, don't update the temp
	if not self.window_open and self._hvac_mode != HVAC_MODE_OFF and self.call_for_heat:
		await set_trv_values(self, 'temperature', target_temp)


async def change_local_calibration(self, calibration):
	# Using on local calibration, update only if the TRV is not in window open mode
	if self.calibration_type == 0 and not self.window_open and self._hvac_mode != HVAC_MODE_OFF:
		await set_trv_values(self, 'local_temperature_calibration', calibration)


async def set_trv_values(self, key, value):
	"""Do necessary actions to set the TRV values."""
	if key == 'temperature':
		await self.hass.services.async_call(
			'climate',
			SERVICE_SET_TEMPERATURE,
			{'entity_id': self.heater_entity_id, 'temperature': value},
			blocking=False
		)
		_LOGGER.debug("better_thermostat %s send %s %s", self.name, key, value)
	elif key == 'system_mode':
		await self.hass.services.async_call(
			'climate',
			SERVICE_SET_HVAC_MODE,
			{'entity_id': self.heater_entity_id, 'hvac_mode': value},
			blocking=False
		)
		_LOGGER.debug("better_thermostat %s send %s %s", self.name, key, value)
	elif key == 'local_temperature_calibration':
		max_calibration = self.hass.states.get(self.local_temperature_calibration_entity).attributes.get('max')
		min_calibration = self.hass.states.get(self.local_temperature_calibration_entity).attributes.get('min')
		if value > max_calibration:
			value = max_calibration
		if value < min_calibration:
			value = min_calibration
		await self.hass.services.async_call(
			'number',
			SERVICE_SET_VALUE,
			{'entity_id': self.local_temperature_calibration_entity, 'value': value},
			blocking=False
		)
		_LOGGER.debug("better_thermostat %s send %s %s", self.name, key, value)
	elif key == 'valve_position':
		await self.hass.services.async_call(
			'number',
			SERVICE_SET_VALUE,
			{'entity_id': self.valve_position_entity, 'value': value},
			blocking=False
		)
		_LOGGER.debug("better_thermostat %s send %s %s", self.name, key, value)
	await asyncio.sleep(1)


async def trv_valve_maintenance(self):
	"""Maintenance of the TRV valve."""
	
	_LOGGER.info("better_thermostat %s: maintenance started", self.name)
	
	self.ignore_states = True
	
	if self.model == "TS0601_thermostat":
		_LOGGER.debug("better_thermostat %s: maintenance will run TS0601_thermostat variant of cycle", self.name)
		
		# get current HVAC mode from HA
		try:
			_last_hvac_mode = self.hass.states.get(self.heater_entity_id).state
		except:
			_LOGGER.error("better_thermostat %s: Could not load current HVAC mode", self.name)
			self.ignore_states = False
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
					'climate', SERVICE_SET_HVAC_MODE, {'entity_id': self.heater_entity_id, 'hvac_mode': 'heat'}, blocking=True
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
			'climate', SERVICE_SET_HVAC_MODE, {'entity_id': self.heater_entity_id, 'hvac_mode': _last_hvac_mode}, blocking=True
		)
		# give the TRV time to process the mode change and report back to HA
		await asyncio.sleep(120)
	
	else:
		
		valve_position_available = False
		# check if there's a valve_position field
		try:
			self.hass.states.get(self.heater_entity_id).attributes.get('valve_position')
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
	
	self.ignore_states = False
	
	# restarting normal heating control immediately
	await control_trv(self)
