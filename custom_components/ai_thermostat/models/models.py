import logging
import math
import os
from pathlib import Path

from homeassistant.components.climate.const import (
	HVAC_MODE_HEAT,
	HVAC_MODE_OFF
)
from homeassistant.util import yaml

from custom_components.ai_thermostat.models.utils import calibration, mode_remap, reverse_modes

_LOGGER = logging.getLogger(__name__)

def convert_inbound_states(self, state):
	try:
		if self.hass.states.get(self.heater_entity_id).attributes.get('device') is not None:
			self.model = self.hass.states.get(self.heater_entity_id).attributes.get('device').get('model')
		else:
			_LOGGER.exception("ai_thermostat: can't read the device model of TVR, Enable include_device_information in z2m or checkout issue #1")
	except RuntimeError:
		_LOGGER.exception("ai_thermostat: error can't get the TRV")
	
	config_file = os.path.dirname(os.path.realpath(__file__)) + '/devices/' + self.model.replace("/", "_") + '.yaml'
	
	if state.get('system_mode') is not None:
		hvac_mode = state.get('system_mode')
	else:
		hvac_mode = HVAC_MODE_HEAT
	
	current_heating_setpoint = self._target_temp
	
	if Path(config_file).is_file():
		config = yaml.load_yaml(config_file)
		self.calibration_type = config.get('calibration_type')
		if (config.get('calibration_type') == 1):
			if state.get('current_heating_setpoint') == 5:
				hvac_mode = HVAC_MODE_OFF
		if config.get('mode_map') is not None and state.get('system_mode') is not None:
			hvac_mode = mode_remap(hvac_mode, reverse_modes(config.get('mode_map')))
	
	return {
		"current_heating_setpoint"     : current_heating_setpoint,
		"local_temperature"            : state.get('local_temperature'),
		"local_temperature_calibration": state.get('local_temperature_calibration'),
		"system_mode"                  : hvac_mode
	}


def convert_outbound_states(self, hvac_mode):
	try:
		if self.hass.states.get(self.heater_entity_id).attributes.get('device') is not None:
			self.model = self.hass.states.get(self.heater_entity_id).attributes.get('device').get('model')
		else:
			_LOGGER.exception("ai_thermostat: can't read the device model of TVR, Enable include_device_information in z2m or checkout issue #1")
	except RuntimeError:
		_LOGGER.exception("ai_thermostat: error can't get the TRV")
	
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	config_file = os.path.dirname(os.path.realpath(__file__)) + '/devices/' + self.model.replace("/", "_") + '.yaml'
	
	if Path(config_file).is_file():
		config = yaml.load_yaml(config_file)
		local_temperature_calibration = calibration(self, config.get('calibration_type'))
		self.calibration_type = config.get('calibration_type')
		if (config.get('calibration_round')):
			local_temperature_calibration = int(math.ceil(local_temperature_calibration))
		if (config.get('calibration_type') == 0):
			current_heating_setpoint = state.get('current_heating_setpoint')
		elif (config.get('calibration_type') == 1):
			current_heating_setpoint = local_temperature_calibration
		
		if state.get('system_mode') is not None:
			if config.get('mode_map') is not None:
				hvac_mode = mode_remap(hvac_mode, config.get('mode_map'))
		else:
			if hvac_mode == HVAC_MODE_OFF:
				current_heating_setpoint = 5
	
	else:
		current_heating_setpoint = self._target_temp
		local_temperature_calibration = int(math.ceil(calibration(self, 0)))
	
	return {
		"current_heating_setpoint"     : current_heating_setpoint,
		"local_temperature"            : state.get('local_temperature'),
		"system_mode"                  : hvac_mode,
		"local_temperature_calibration": local_temperature_calibration
	}
