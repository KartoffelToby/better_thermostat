import logging
import asyncio

from homeassistant.components.climate.const import (SERVICE_SET_HVAC_MODE, SERVICE_SET_TEMPERATURE)
from homeassistant.components.number.const import (SERVICE_SET_VALUE)
from homeassistant.components.climate.const import (HVAC_MODE_HEAT, HVAC_MODE_OFF)
from homeassistant.const import (ATTR_TEMPERATURE)

_LOGGER = logging.getLogger(__name__)

async def set_target_temperature(self, **kwargs):
	temperature = kwargs.get(ATTR_TEMPERATURE)
	if temperature is None:
		return
	self._target_temp = temperature
	self.async_write_ha_state()
	await self._async_control_heating()

async def set_hvac_mode(self, hvac_mode):
	if hvac_mode == HVAC_MODE_HEAT:
		self._hvac_mode = HVAC_MODE_HEAT
	elif hvac_mode == HVAC_MODE_OFF:
		self._hvac_mode = HVAC_MODE_OFF
	else:
		_LOGGER.error("better_thermostat %s: Unsupported hvac_mode %s", self.name, hvac_mode)
	self.async_write_ha_state()
	await self._async_control_heating()

async def change_local_calibration():
	return True

async def set_trv_values(self, key, value):
	"""Do necessary actions to set the TRV values."""
	if key == 'temperature':
		await self.hass.services.async_call('climate', SERVICE_SET_TEMPERATURE, {'entity_id': self.heater_entity_id, 'temperature': value}, blocking=True)
		_LOGGER.debug("better_thermostat send %s %s", key, value)
	elif key == 'system_mode':
		await self.hass.services.async_call('climate', SERVICE_SET_HVAC_MODE, {'entity_id': self.heater_entity_id, 'hvac_mode': value}, blocking=True)
		_LOGGER.debug("better_thermostat send %s %s", key, value)
	elif key == 'local_temperature_calibration':
		max_calibration = self.hass.states.get(self.local_temperature_calibration_entity).attributes.get('max')
		min_calibration = self.hass.states.get(self.local_temperature_calibration_entity).attributes.get('min')
		if value > max_calibration:
			value = max_calibration
		if value < min_calibration:
			value = min_calibration
		await self.hass.services.async_call('number', SERVICE_SET_VALUE, {'entity_id': self.local_temperature_calibration_entity, 'value': value}, blocking=True)
		_LOGGER.debug("better_thermostat send %s %s", key, value)
	elif key == 'valve_position':
		await self.hass.services.async_call('number', SERVICE_SET_VALUE, {'entity_id': self.valve_position_entity, 'value': value}, blocking=True)
		_LOGGER.debug("better_thermostat send %s %s", key, value)
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
	await self._async_control_heating()