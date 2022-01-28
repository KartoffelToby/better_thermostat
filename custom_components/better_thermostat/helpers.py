"""Helper functions for the Better Thermostat component."""

import asyncio
import logging
import re
from datetime import datetime

from homeassistant.components.climate.const import (SERVICE_SET_HVAC_MODE, SERVICE_SET_TEMPERATURE)
from homeassistant.components.number.const import (SERVICE_SET_VALUE)
from homeassistant.helpers import device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)


def check_float(potential_float):
	"""Check if a string is a float."""
	try:
		float(potential_float)
		return True
	except ValueError:
		return False


def convert_time(time_string):
	"""Convert a time string to a datetime object."""
	try:
		_current_time = datetime.now()
		_get_hours_minutes = datetime.strptime(time_string, "%H:%M")
		return _current_time.replace(hour=_get_hours_minutes.hour, minute=_get_hours_minutes.minute, second=0, microsecond=0)
	except ValueError:
		return None


def convert_decimal(decimal_string):
	"""Convert a decimal string to a float."""
	try:
		return float(format(float(decimal_string), '.1f'))
	except ValueError:
		return None

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
