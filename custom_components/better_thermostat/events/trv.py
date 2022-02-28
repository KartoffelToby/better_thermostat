import logging

from homeassistant.components.climate.const import HVAC_MODE_HEAT, HVAC_MODE_OFF
from homeassistant.core import callback, State

from ..controlling import control_trv
from ..models.models import convert_inbound_states
from ..models.utils import convert_to_float

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
	"""Processes TRV status updates

	Parameters
	----------
	self : 
	event : 

	Returns
	-------

	"""
	if self.startup_running:
		_LOGGER.debug(f"better_thermostat {self.name}: skipping trigger_trv_change because startup is running")
		return
	
	force_update = False
	
	old_state = event.data.get("old_state")
	new_state = event.data.get("new_state")
	
	_LOGGER.debug(f"better_thermostat {self.name}: trigger_trv_change: old_state: {old_state.state} new_state: {new_state.state}")
	
	if not all([new_state, old_state, new_state.attributes]):
		_LOGGER.debug(f"better_thermostat {self.name}: TRV update contained not all necessary data for processing, skipping")
		return
	
	if not isinstance(new_state, State) or not isinstance(old_state, State):
		_LOGGER.debug(f"better_thermostat {self.name}: TRV update contained not a State, skipping")
		return
	
	try:
		convert_inbound_states(self, new_state)
	except TypeError:
		_LOGGER.debug(f"better_thermostat {self.name}: remapping TRV state failed, skipping")
		return
	
	# if flag is on, we won't do any further processing
	if self.ignore_states:
		_LOGGER.debug(f"better_thermostat {self.name}: skipping trigger_trv_change because ignore_states is true")
		return
	
	new_decoded_system_mode = str(new_state.state)
	
	_LOGGER.debug(
		f"better_thermostat {self.name}: trigger_trv_change: new_decoded_system_mode: {new_decoded_system_mode}, expected system mode: {self._trv_hvac_mode}"
		)
	
	if new_decoded_system_mode not in (HVAC_MODE_OFF, HVAC_MODE_HEAT):
		# not an valid mode, overwriting
		_LOGGER.debug(f"better_thermostat {self.name}: TRV's decoded TRV mode is not valid, overwriting with correct system mode")
		
		force_update = True
	
	# check if this change is what we expected (if not already an update is forced)
	if not force_update and self._trv_hvac_mode != new_decoded_system_mode:
		_LOGGER.warning(
			f"better_thermostat {self.name}: TRV is not in the expected mode, we will force an update"
		)
		force_update = True
	
	if force_update:
		await control_trv(self)
		return
	
	# we only read user input at the TRV on mode 0
	if self.calibration_type != 0:
		return
	
	# we only read setpoint changes from TRV if we are in heating mode
	if self._trv_hvac_mode == HVAC_MODE_OFF:
		return
	
	if _new_heating_setpoint := convert_to_float(new_state.attributes.get('current_heating_setpoint'), self.name, "trigger_trv_change()") \
	                            is None:
		return
	
	if _new_heating_setpoint < self._min_temp or self._max_temp < _new_heating_setpoint:
		_LOGGER.warning(f"better_thermostat {self.name}: New TRV setpoint outside of range, overwriting it")
		
		if _new_heating_setpoint < self._min_temp:
			_new_heating_setpoint = self._min_temp
		else:
			_new_heating_setpoint = self._max_temp
		
		self._target_temp = _new_heating_setpoint
		
		await control_trv(self)
	self.async_write_ha_state()


def update_valve_position(self, valve_position):
	"""Updates the stored valve position and triggers async tasks waiting for this

	Parameters
	----------
	self : 
		FIXME
	valve_position :
		the new valve position

	Returns
	-------
	None 
	"""
	
	if valve_position is not None:
		_LOGGER.debug(f"better_thermostat {self.name}: Updating valve position to {valve_position}")
		self._last_reported_valve_position = valve_position
		self._last_reported_valve_position_update_wait_lock.release()
	else:
		_LOGGER.debug(f"better_thermostat {self.name}: Valve position is None, skipping")
