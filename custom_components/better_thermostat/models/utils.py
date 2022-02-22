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
	
	if _current_trv_temp := state.get('current_temperature') is None:
		_current_trv_temp = 0
	else:
		_current_trv_temp = float(_current_trv_temp)
		
	if _current_trv_calibration := state.get('current_temperature_calibration') is None:
		_current_trv_calibration = 0
	else:
		_current_trv_calibration = float(_current_trv_calibration)
		
	_new_local_calibration = self._cur_temp - _current_trv_temp + _current_trv_calibration
	return _new_local_calibration

def calculate_setpoint_override(self):
	# FIXME: Write docstring
	
	state = self.hass.states.get(self.heater_entity_id).attributes
	
	if _current_trv_temp := state.get('current_temperature') is None:
		_current_trv_temp = 0
	else:
		_current_trv_temp = float(_current_trv_temp)
		
	_calibrated_setpoint = self._target_temp - self._cur_temp + _current_trv_temp
	if _calibrated_setpoint < float(self._TRV_min_temp):
		_calibrated_setpoint = float(self._TRV_min_temp)
	if _calibrated_setpoint > float(self._TRV_max_temp):
		_calibrated_setpoint = float(self._TRV_max_temp)
	
	return _calibrated_setpoint
