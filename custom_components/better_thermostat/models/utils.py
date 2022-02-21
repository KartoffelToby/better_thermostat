"""Utility functions for the Better Thermostat."""

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
		return calculate_setpoint_override(self)
	if calibration_type == 0:
		return calculate_local_setpoint_delta(self)

def calculate_local_setpoint_delta(self):
	# FIXME: Write docstring
	
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	if _local_temp := state.get('local_temperature') is None:
		_local_temp = 0
	else:
		_local_temp = float(_local_temp)
		
	if _local_temp_calibration := state.get('local_temperature_calibration') is None:
		_local_temp_calibration = 0
	else:
		_local_temp_calibration = float(_local_temp_calibration)
		
	new_local_calibration = self._cur_temp - _local_temp + _local_temp_calibration
	return new_local_calibration

def calculate_setpoint_override(self):
	# FIXME: Write docstring
	
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	if _local_temp := state.get('local_temperature') is None:
		_local_temp = 0
	else:
		_local_temp = float(_local_temp)
		
	calibrated_setpoint = self._target_temp - self._cur_temp + _local_temp
	if calibrated_setpoint < float(self._TRV_min_temp):
		calibrated_setpoint = float(self._TRV_min_temp)
	if calibrated_setpoint > float(self._TRV_max_temp):
		calibrated_setpoint = float(self._TRV_max_temp)
	
	return calibrated_setpoint
