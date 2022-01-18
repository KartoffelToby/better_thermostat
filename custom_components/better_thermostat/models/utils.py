"""Utility functions for the Better Thermostat."""

from ..helpers import convert_decimal


def mode_remap(hvac_mode, modes):
	"""Remap HVAC mode to better mode."""
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


def calibration(self, calibration_type):
	"""Select calibration function based on calibration type."""
	if calibration_type == 1:
		return temperature_calibration(self)
	if calibration_type == 0:
		return default_calibration(self)


def default_calibration(self):
	# FIXME: Write docstring
	
	state = self.hass.states.get(self.config_heater_entity).attributes
	new_calibration = float(
		(float(self._current_room_temperature_sensor_value) - float(state.get('local_temperature'))) + float(state.get('local_temperature_calibration'))
	)
	return convert_decimal(new_calibration)


def temperature_calibration(self):
	# FIXME: Write docstring
	
	state = self.hass.states.get(self.config_heater_entity).attributes
	new_calibration = abs(float(round((float(self._current_temperature_setpoint) - float(self._current_room_temperature_sensor_value)) + float(state.get('local_temperature')), 2)))
	if new_calibration < float(self._minimal_set_temperature):
		new_calibration = float(self._minimal_set_temperature)
	if new_calibration > float(self._maximal_set_temperature):
		new_calibration = float(self._maximal_set_temperature)
	
	return new_calibration
