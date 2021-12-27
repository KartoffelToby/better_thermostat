import asyncio
import logging
from datetime import datetime, timedelta

from custom_components.ai_thermostat.helpers import convert_decimal, set_trv_values

_LOGGER = logging.getLogger(__name__)


def mode_remap(hvac_mode, modes):
	if modes is None:
		return hvac_mode
	if modes.get(hvac_mode) is not None:
		return modes.get(hvac_mode)
	else:
		return hvac_mode

def reverse_modes(modes):
	changed_dict = {}
	for key, value in modes.items():
		changed_dict[value] = key
	return changed_dict


def calibration(self, type):
	if type == 1:
		return temperature_calibration(self)
	if type == 0:
		return default_calibration(self)


def default_calibration(self):
	state = self.hass.states.get(self.heater_entity_id).attributes
	new_calibration = float((float(self._cur_temp) - float(state.get('local_temperature'))) + float(state.get('local_temperature_calibration')))
	return convert_decimal(new_calibration)


async def evaluate_dampening(self, calibration):
	state = self.hass.states.get(self.heater_entity_id).attributes
	if (datetime.now() > (self.last_dampening_timestamp + timedelta(minutes=15))) and state.get(
			'system_mode') is not None and self._target_temp is not None and self._cur_temp is not None and not self.night_status:
		check_dampening = (float(self._target_temp) - 0.5) < float(self._cur_temp)
		if check_dampening:
			self.ignore_states = True
			_LOGGER.debug("Dampening started")
			await set_trv_values(self,'temperature', float(5))
			await asyncio.sleep(60)
			await set_trv_values(self,'temperature', float(calibration))
			self.last_dampening_timestamp = datetime.now()
			self.ignore_states = False


def temperature_calibration(self):
	state = self.hass.states.get(self.heater_entity_id).attributes
	new_calibration = abs(float(round((float(self._target_temp) - float(self._cur_temp)) + float(state.get('local_temperature')), 2)))
	if new_calibration < float(self._min_temp):
		new_calibration = float(self._min_temp)
	if new_calibration > float(self._max_temp):
		new_calibration = float(self._max_temp)
	
	#loop = asyncio.get_event_loop()
	#loop.create_task(evaluate_dampening(self, new_calibration))
	
	return new_calibration
