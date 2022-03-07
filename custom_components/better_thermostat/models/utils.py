"""Utility functions for the Better Thermostat."""

import logging
from typing import Union

from homeassistant.components.climate.const import HVAC_MODE_AUTO, HVAC_MODE_HEAT

_LOGGER = logging.getLogger(__name__)


def load_bool_from_config(self, key: str, default=None) -> bool:
	"""Load a boolean from the config."""
	
	if self._config is None:
		raise TypeError("load_bool_from_config() could not find config, cannot convert")
	
	try:
		value = self._config.get(key)
	except KeyError:
		return default
	
	if isinstance(value, bool):
		return value
	elif isinstance(value, str) and value.lower() == 'false':
		return False
	elif isinstance(value, str) and value.lower() == 'true':
		return True
	else:
		return default


def device_has_swapped_modes(self) -> bool:
	"""Check config if device has swapped HVAC modes"""
	try:
		device_has_quirks = load_bool_from_config(self, "heat_auto_swapped")
	except TypeError:
		_LOGGER.error(f"better thermostat {self.name}: Could not load config for heat_auto_swapped")
		device_has_quirks = False
	
	if isinstance(device_has_quirks, bool):
		return device_has_quirks
	else:
		return False


def mode_remap(self, hvac_mode: str) -> str:
	"""Remap HVAC mode to correct mode if nessesary.

	Parameters
	----------
	self : 
		FIXME
	hvac_mode : str
		HVAC mode to be remapped

	Returns
	-------
	str
		remapped mode according to device's quirks
	"""
	
	device_has_quirks = device_has_swapped_modes(self)
	
	if hvac_mode == HVAC_MODE_HEAT and device_has_quirks:
		return HVAC_MODE_AUTO
	elif hvac_mode == HVAC_MODE_AUTO and device_has_quirks:
		return HVAC_MODE_HEAT
	else:
		return hvac_mode


def calculate_local_setpoint_delta(self) -> Union[float, None]:
	"""Calculate local delta to adjust the setpoint of the TRV based on the air temperature of the external sensor.
	
	This calibration is for devices with local calibration option, it syncs the current temperature of the TRV to the target temperature of
	the external sensor.

	Parameters
	----------
	self : 
		self instance of better_thermostat

	Returns
	-------
	float
		new local calibration delta
	"""
	
	_trv_state_attributes = self.hass.states.get(self.heater_entity_id).attributes
	_calibration_state = self.hass.states.get(self.local_temperature_calibration_entity).state
	_context = "calculate_local_setpoint_delta()"
	
	_current_trv_temp = convert_to_float(_trv_state_attributes.get('current_temperature'), self.name, _context)
	_current_trv_calibration = convert_to_float(_calibration_state, self.name, _context)
	
	if None in (_current_trv_calibration, self._cur_temp, _current_trv_temp):
		_LOGGER.warning(
			f"better thermostat {self.name}: Could not calculate local setpoint delta in {_context}:"
			f" current_trv_calibration: {_current_trv_calibration}, current_trv_temp: {_current_trv_temp}, cur_temp: {self._cur_temp}"
		)
		return None
	
	_new_local_calibration = self._cur_temp - _current_trv_temp + _current_trv_calibration
	return _new_local_calibration


def calculate_setpoint_override(self) -> Union[float, None]:
	"""Calculate new setpoint for the TRV based on its own temperature measurement and the air temperature of the external sensor.
	
	This calibration is for devices with no local calibration option, it syncs the target temperature of the TRV to a new target
	temperature based on the current temperature of the external sensor.

	Parameters
	----------
	self : 
		self instance of better_thermostat

	Returns
	-------
	float
		new target temp with calibration
	"""
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	_context = "calculate_setpoint_override()"
	
	_current_trv_temp = convert_to_float(state.get('current_temperature'), self.name, _context)
	
	if None in (self._target_temp, self._cur_temp, _current_trv_temp):
		return None
	
	_calibrated_setpoint = round_to_half_degree(self._target_temp - self._cur_temp + _current_trv_temp)
	
	# check if new setpoint is inside the TRV's range, else set to min or max
	if _calibrated_setpoint < self._TRV_min_temp:
		_calibrated_setpoint = self._TRV_min_temp
	if _calibrated_setpoint > self._TRV_max_temp:
		_calibrated_setpoint = self._TRV_max_temp
	
	return _calibrated_setpoint


def convert_to_float(value: Union[str, int, float], instance_name: str, context: str) -> Union[float, None]:
	"""Convert value to float or print error message.

	Parameters
	----------
	value : str, int, float
		the value to convert to float
	instance_name : str
		the name of the instance thermostat
	context : str
		the name of the function which is using this, for printing an error message

	Returns
	-------
	float
		the converted value
	None
		If error occurred and cannot convert the value.
	"""
	if isinstance(value, float):
		return value
	else:
		try:
			return float(value)
		except (ValueError, TypeError, AttributeError, KeyError):
			_LOGGER.error(f"better thermostat {instance_name}: Could not convert '{value}' to float in {context}")
			return None


def round_to_half_degree(value: Union[int, float, None]) -> Union[float, int, None]:
	"""Rounds numbers to the nearest n.5/n.0

	Parameters
	----------
	value : int, float
		input value

	Returns
	-------
	float, int
		either an int, if input was an int, or a float rounded to n.5/n.0

	"""
	if value is None:
		return None
	elif isinstance(value, float):
		return round(value * 2) / 2
	elif isinstance(value, int):
		return value
