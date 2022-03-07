"""Device model handing and quirk detection."""

import logging
import os
import re
from pathlib import Path
from typing import Union

from homeassistant.components.climate.const import HVAC_MODE_OFF
from homeassistant.core import State
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import yaml

from .utils import calculate_local_setpoint_delta, calculate_setpoint_override, mode_remap, round_to_half_degree

_LOGGER = logging.getLogger(__name__)


def convert_inbound_states(self, state: State):
	"""Convert hvac mode in a thermostat state from HA

	Parameters
	----------
	self : 
		FIXME
	state : State
		Inbound thermostat state, which will be modified

	Returns
	-------
	None
	"""
	
	if state is None:
		raise TypeError("convert_inbound_states() received None state, cannot convert")
	
	if state.attributes is None or state.state is None:
		raise TypeError("convert_inbound_states() received None state, cannot convert")
	
	if self._config is None:
		raise TypeError("convert_inbound_states() could not find config, cannot convert")
	
	_LOGGER.debug(f"convert_inbound_states() received state: {str(state.state)}")
	
	state.state = mode_remap(self, str(state.state))
	
	_LOGGER.debug(f"convert_inbound_states() wrote state: {str(state.state)}")


async def get_device_model(self):
	"""Fetches the device model from HA.

	Parameters
	----------
	self : 
		self instance of better_thermostat

	Returns
	-------
	string
		the name of the thermostat model
	"""
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
		except (RuntimeError, ValueError, AttributeError, KeyError, TypeError, NameError, IndexError):
			_LOGGER.error(
				"better_thermostat %s: could not read device model of your TVR. Make sure this device exists in Home Assistant.",
				self.name
			)
			return None
	else:
		return self.model


def load_device_config(self) -> bool:
	"""Load device config from file.

	Parameters
	----------
	self : 
		self instance of better_thermostat

	Returns
	-------
	bool
		True if config was loaded, False if not.

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


def convert_outbound_states(self, hvac_mode) -> Union[dict, None]:
	"""Creates the new outbound thermostat state.
	
	Parameters
	----------
	self : 
		self instance of better_thermostat
	hvac_mode : 
		the HA mode to convert to

	Returns
	-------
	dict
		A dictionary containing the new outbound thermostat state containing the following keys:
			current_heating_setpoint: float
			local_temperature: float
			local_temperature_calibration: float
			system_mode: string
	None
		In case of an error.

	"""
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	_new_local_calibration = None
	_new_heating_setpoint = None
	
	if self._config is None:
		_LOGGER.warning("better_thermostat %s: no matching device config loaded, talking to the TRV using fallback mode", self.name)
		_new_heating_setpoint = self._target_temp
		_new_local_calibration = round_to_half_degree(calculate_local_setpoint_delta(self))
		if _new_local_calibration is None:
			return None
	
	else:
		_calibration_type = self._config.get('calibration_type')
		
		if _calibration_type is None:
			_LOGGER.warning(
				"better_thermostat %s: no calibration type found in device config, talking to the TRV using fallback mode",
				self.name
			)
			_new_heating_setpoint = self._target_temp
			_new_local_calibration = round_to_half_degree(calculate_local_setpoint_delta(self))
			if _new_local_calibration is None:
				return None
		
		else:
			if _calibration_type == 0:
				_round_calibration = self._config.get('calibration_round')
				
				if _round_calibration is not None and (
					(isinstance(_round_calibration, str) and _round_calibration.lower() == 'true') or _round_calibration is True):
					_new_local_calibration = round_to_half_degree(calculate_local_setpoint_delta(self))
					if _new_local_calibration is None:
						return None
				else:
					_new_local_calibration = calculate_local_setpoint_delta(self)
				
				_new_heating_setpoint = self._target_temp
			
			elif _calibration_type == 1:
				_new_heating_setpoint = calculate_setpoint_override(self)
				
			_has_system_mode = self._config.get('has_system_mode')
			_system_mode = self._config.get('system_mode')
			
			if isinstance(_has_system_mode, str) and _has_system_mode.lower() == 'false':
				# we expect no system mode
				_has_system_mode = False
			elif isinstance(_has_system_mode, str) and _has_system_mode.lower() == 'true':
				# we expect a system mode
				_has_system_mode = True
			
			# Handling different devices with or without system mode reported or contained in the device config
			
			if _has_system_mode is True or (_has_system_mode is None and _system_mode is not None):
				hvac_mode = mode_remap(self, hvac_mode)
			
			elif _has_system_mode is True and _system_mode is None:
				_LOGGER.error(
					f"better_thermostat {self.name}: device reports no system mode, while device config expects one. No changes to TRV "
					f"will be made until device reports a system mode (again)"
					)
				return None
			
			elif _has_system_mode is False:
				if _system_mode is not None:
					_LOGGER.warning(
						f"better_thermostat {self.name}: device config expects no system mode, while the device has one. Device system mode will be ignored"
					)
				if hvac_mode == HVAC_MODE_OFF:
					_new_heating_setpoint = 5
				hvac_mode = None
			
			elif _has_system_mode is None and _system_mode is None:
				if hvac_mode == HVAC_MODE_OFF:
					_LOGGER.info(
						f"better_thermostat {self.name}: sending 5°C to the TRV because this device has no system mode and heater should be off"
					)
					_new_heating_setpoint = 5
				hvac_mode = None
	
	return {
		"current_heating_setpoint"     : _new_heating_setpoint,
		"local_temperature"            : state.get('current_temperature'),
		"system_mode"                  : hvac_mode,
		"local_temperature_calibration": _new_local_calibration
	}
