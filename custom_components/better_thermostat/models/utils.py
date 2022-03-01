"""Utility functions for the Better Thermostat."""

import logging
from typing import Union

_LOGGER = logging.getLogger(__name__)


def mode_remap(hvac_mode: str, modes: dict) -> str:
	"""Remap HVAC mode to correct mode

	Parameters
	----------
	hvac_mode : str
		HVAC mode to be remapped
	modes : 
		Dictionary with HVAC mode remappings

	Returns
	-------
	str
		remapped mode according to device's quirks
	"""
	
	if modes is None:
		return hvac_mode
	if modes.get(hvac_mode) is not None:
		return modes.get(hvac_mode)
	else:
		return hvac_mode


def reverse_modes(modes):
	"""Reverse HVAC modes."""
	changed_dict = {}
	for key, value in modes.items():
		changed_dict[value] = key
	return changed_dict


def calculate_local_setpoint_delta(self) -> Union[float, None]:
	"""Calculate local delta to adjust the setpoint of the TRV based on the air temperature of the external sensor.
	
	This calibration is for devices with local calibration option, it syncs the current temperature of the TRV to the target temperature of
	the external sensor.
	"""
	
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	_context = "calculate_local_setpoint_delta()"
	
	_current_trv_temp = convert_to_float(state.get('current_temperature'), self.name, _context)
	_current_trv_calibration = convert_to_float(state.get('local_temperature_calibration'), self.name, _context)
	
	if not all([_current_trv_calibration, self._cur_temp, _current_trv_temp]):
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
	"""
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	_context = "calculate_setpoint_override()"
	
	_current_trv_temp = convert_to_float(state.get('current_temperature'), self.name, _context)
	
	if not all([self._target_temp, self._cur_temp, _current_trv_temp]):
		return None
	
	_calibrated_setpoint = round_to_half_degree(self._target_temp - self._cur_temp + _current_trv_temp)
	
	# check if new setpoint is inside the TRV's range, else set to min or max
	if _calibrated_setpoint < self._TRV_min_temp:
		_calibrated_setpoint = self._TRV_min_temp
	if _calibrated_setpoint > self._TRV_max_temp:
		_calibrated_setpoint = self._TRV_max_temp
	
	return _calibrated_setpoint


def convert_to_float(value: Union[str, int, float], instance_name: str, context: str) -> Union[float, None]:
	"""Convert value to float or print error message."""
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
