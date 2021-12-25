import asyncio
import logging
from datetime import datetime, timedelta

from custom_components.ai_thermostat.helpers import convert_decimal

_LOGGER = logging.getLogger(__name__)


def mode_remap(hvac_mode, modes):
	if modes is None:
		return hvac_mode
	if modes.get(hvac_mode) is not None:
		return modes.get(hvac_mode)
	else:
		return hvac_mode


def swapList(list):
	changedDict = {}
	for key, value in list.items():
		changedDict[value] = key
	return changedDict


def calibration(self, type):
	if type == 1:
		return temperature_calibration(self)
	if type == 0:
		return default_calibration(self)


def default_calibration(self):
	state = self.hass.states.get(self.heater_entity_id).attributes
	# new_calibration = int(math.ceil((math.floor(float(self._cur_temp)) - round(float(state.get('local_temperature')))) + round(float(state.get('local_temperature_calibration')))))
	# temp range fix
	new_calibration = float((float(self._cur_temp) - float(state.get('local_temperature'))) + float(state.get('local_temperature_calibration')))
	if new_calibration > 0 and new_calibration < 1:
		new_calibration = round(new_calibration)
	if new_calibration < -30:
		new_calibration = -30
	if new_calibration > 30:
		new_calibration = 30
	
	return convert_decimal(new_calibration)


async def evaluate_dampening(self, calibration):
	state = self.hass.states.get(self.heater_entity_id).attributes
	mqtt = self.hass.components.mqtt
	if (datetime.now() > (self.last_dampening_timestamp + timedelta(minutes=15))) and state.get(
			'system_mode') is not None and self._target_temp is not None and self._cur_temp is not None and not self.night_status:
		check_dampening = (float(self._target_temp) - 0.5) < float(self._cur_temp)
		if check_dampening:
			self.ignore_states = True
			_LOGGER.debug("Dampening started")
			await mqtt.async_publish(self.hass, 'zigbee2mqtt/' + state.get('device').get('friendlyName') + '/set/current_heating_setpoint', float(5), 0, False)
			await asyncio.sleep(60)
			await mqtt.async_publish(self.hass, 'zigbee2mqtt/' + state.get('device').get('friendlyName') + '/set/current_heating_setpoint', float(calibration), 0, False)
			self.last_dampening_timestamp = datetime.now()
			self.ignore_states = False


def temperature_calibration(self):
	state = self.hass.states.get(self.heater_entity_id).attributes
	new_calibration = abs(float(round((float(self._target_temp) - float(self._cur_temp)) + float(state.get('local_temperature')), 2)))
	if new_calibration < float(self._min_temp):
		new_calibration = float(self._min_temp)
	if new_calibration > float(self._max_temp):
		new_calibration = float(self._max_temp)
	
	# loop = asyncio.get_event_loop()
	# loop.create_task(evaluate_dampening(self,new_calibration))
	
	return new_calibration
