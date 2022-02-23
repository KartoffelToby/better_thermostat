"""Device model handing and quirk detection."""

import logging
import os
import re
from pathlib import Path

from homeassistant.components.climate.const import (HVAC_MODE_HEAT, HVAC_MODE_OFF)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import yaml

from .utils import calibration, mode_remap, reverse_modes

_LOGGER = logging.getLogger(__name__)


def convert_inbound_states(self, state):
	"""Convert inbound thermostat state to HA state."""
	
	if state.get('system_mode') is not None:
		hvac_mode = state.get('system_mode')
	else:
		hvac_mode = HVAC_MODE_HEAT
	
	current_heating_setpoint = self._target_temp
	
	if self._config is not None:
		self.calibration_type = self._config.get('calibration_type')
		if self._config.get('calibration_type') == 1:
			if state.get('current_heating_setpoint') == 5:
				hvac_mode = HVAC_MODE_OFF
		if self._config.get('mode_map') is not None and state.get('system_mode') is not None:
			hvac_mode = mode_remap(hvac_mode, reverse_modes(self._config.get('mode_map')))
	
	return {
		"current_heating_setpoint"     : current_heating_setpoint,
		"local_temperature"            : state.get('current_temperature'),
		"local_temperature_calibration": state.get('local_temperature_calibration'),
		"system_mode"                  : hvac_mode}


async def get_device_model(self):
	"""Fetches the device model from HA."""
	if self.model is None:
		try:
			entity_reg = await er.async_get_registry(self.hass)
			entry = entity_reg.async_get(self.heater_entity_id)
			dev_reg = await dr.async_get_registry(self.hass)
			device = dev_reg.async_get(entry.device_id)
			try:
				# Z2M reports the device name as a long string with the actual model name in braces, we need to extract it
				return re.search('\((.+?)\)', device.model).group(1)
			except AttributeError:
				# Other climate integrations might report the model name plainly, need more infos on this
				return device.model
		except (RuntimeError, ValueError, AttributeError, KeyError, TypeError, NameError, IndexError) as e:
			_LOGGER.error(
				"better_thermostat %s: could not read device model of your TVR. Make sure this device exists in Home Assistant.",
				self.name
			)
			return None
	else:
		return self.model


def load_device_config(self) -> bool:
	"""Load device config from file.
	
	Returns: True if config was loaded, False if not.
	"""
	
	if self.model is None:
		return False
	
	config_file = os.path.dirname(os.path.realpath(__file__)) + '/devices/' + self.model.replace("/", "_") + '.yaml'
	
	if Path(config_file).is_file():
		self._config = yaml.load_yaml(config_file)
		return True
	else:
		_LOGGER.error(
			f"better_thermostat {self.name}: could not find device config for your TVR. Make sure this device exists in Home Assistant."
		)
		self._config = None
		return False


def convert_outbound_states(self, hvac_mode) -> dict:
	"""Convert HA state to outbound thermostat state."""
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	if self._config is None:
		current_heating_setpoint = self._target_temp
		local_temperature_calibration = round(calibration(self, 0))
	
	else:
		current_heating_setpoint = None
		
		local_temperature_calibration = calibration(self, self._config.get('calibration_type'))
		self.calibration_type = self._config.get('calibration_type')
		if self._config.get('calibration_round'):
			local_temperature_calibration = round(local_temperature_calibration)
		if self._config.get('calibration_type') == 0:
			current_heating_setpoint = self._target_temp
		elif self._config.get('calibration_type') == 1:
			current_heating_setpoint = local_temperature_calibration
			local_temperature_calibration = None
		
		if state.get('system_mode') is not None:
			if self._config.get('mode_map') is not None:
				hvac_mode = mode_remap(hvac_mode, self._config.get('mode_map'))
		else:
			if hvac_mode == HVAC_MODE_OFF:
				current_heating_setpoint = 5
	
	return {
		"current_heating_setpoint"     : current_heating_setpoint,
		"local_temperature"            : state.get('current_temperature'),
		"system_mode"                  : hvac_mode,
		"local_temperature_calibration": local_temperature_calibration
	}
