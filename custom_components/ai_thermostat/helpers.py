from datetime import datetime
from homeassistant.components.climate.const import (
	SERVICE_SET_TEMPERATURE,
	SERVICE_SET_HVAC_MODE,
)
from homeassistant.components.number.const import (
	SERVICE_SET_VALUE,
)
import asyncio

def check_float(potential_float):
	try:
		float(potential_float)
		return True
	except ValueError:
		return False


def convert_time(time_string):
	try:
		currentTime = datetime.now()
		getHoursMinutes = datetime.strptime(time_string, "%H:%M")
		return currentTime.replace(hour=getHoursMinutes.hour, minute=getHoursMinutes.minute, second=0, microsecond=0)
	except ValueError:
		return None


def convert_decimal(decimal_string):
	try:
		return float(format(float(decimal_string), '.1f'))
	except ValueError:
		return None

async def set_trv_values(self, key, value):
	if key == 'temperature':
		await self.hass.services.async_call('climate', SERVICE_SET_TEMPERATURE, {'entity_id': self.heater_entity_id, 'temperature': value}, blocking=True)
	elif key == 'system_mode':
		await self.hass.services.async_call('climate', SERVICE_SET_HVAC_MODE, {'entity_id': self.heater_entity_id, 'hvac_mode': value}, blocking=True)
	elif key == 'local_temperature_calibration':
		local_calibration_entity = self.heater_entity_id.replace('climate.', 'number.') + '_local_temperature_calibration'
		max_calibration = self.hass.states.get(local_calibration_entity).attributes.get('max')
		min_calibration = self.hass.states.get(local_calibration_entity).attributes.get('min')
		if value > max_calibration:
			value = max_calibration
		if value < min_calibration:
			value = min_calibration
		await self.hass.services.async_call('number', SERVICE_SET_VALUE, {'entity_id': local_calibration_entity, 'value': value}, blocking=True)
	elif key == 'valve_position':
		valve_position_entity = self.heater_entity_id.replace('climate.', 'number.') + '_valve_position'
		await self.hass.services.async_call('number', SERVICE_SET_VALUE, {'entity_id': valve_position_entity, 'value': value}, blocking=True)
	await asyncio.sleep(1)