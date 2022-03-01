import logging

from homeassistant.components.climate.const import HVAC_MODE_HEAT, HVAC_MODE_OFF
from homeassistant.core import callback

from ..const import ATTR_VALVE_POSITION
from ..controlling import control_trv
from ..models.models import convert_inbound_states
from ..models.utils import convert_to_float

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_trv_change(self, event):
	"""Process TRV status updates"""
	if self.startup_running:
		_LOGGER.debug(f"better_thermostat {self.name}: skipping trigger_trv_change because startup is running")
		return
	
	force_update = False
	
	old_state = event.data.get("old_state")
	new_state = event.data.get("new_state")
	
	if not all([new_state, old_state, new_state.attributes]):
		_LOGGER.debug(f"better_thermostat {self.name}: TRV update contained not all necessary data for processing, skipping")
		return
	
	remapped_state = convert_inbound_states(self, new_state)
	
	update_valve_position(self, remapped_state.get(ATTR_VALVE_POSITION))
	
	# if flag is on, we won't do any further processing
	if self.ignore_states:
		_LOGGER.debug(f"better_thermostat {self.name}: skipping trigger_trv_change because ignore_states is true")
		return
	
	# system mode has been changed
	if old_state != new_state:
		_LOGGER.debug(
			f"better_thermostat {self.name}: TRV System Mode changed from '{old_state}' to '{new_state}'"
		)
		
		new_decoded_system_mode = remapped_state.get('system_mode')
		_LOGGER.debug(
			f"better_thermostat {self.name}: Expected decoded TRV mode: '{self._hvac_mode}'. TRV's decoded TRV mode: '{new_decoded_system_mode}'"
		)
		
		if new_decoded_system_mode is None or new_decoded_system_mode not in (HVAC_MODE_OFF, HVAC_MODE_HEAT):
			# we don't understand this system mode, so we'll force an update to overwrite it
			_LOGGER.debug(f"better_thermostat {self.name}: TRV's decoded TRV mode is not valid, overwriting with correct system mode")
			
			force_update = True
			_LOGGER.debug(
				f"better_thermostat {self.name}: Could not parse new system mode from TRV, forcing an update with the "
				f"expected system mode: {self._hvac_mode}"
			)
		
		# check if this change is what we expected (if not already an update is forced)
		if not force_update and self._hvac_mode != new_decoded_system_mode:
			_LOGGER.warning(
				f"better_thermostat {self.name}: TRV is not in the expected mode, we will force an update"
			)
			force_update = True
		
		if force_update:
			await control_trv(self)
			return
	
	else:
		_LOGGER.debug(f"better_thermostat {self.name}: trigger_trv_change detected no system mode change")
	
	# we only read user input at the TRV on mode 0
	if self.calibration_type != 0:
		return
	
	# we only read setpoint changes from TRV if we are in heating mode
	if self._hvac_mode == HVAC_MODE_OFF:
		return
	
	if _new_heating_setpoint := convert_to_float(
		new_state.attributes.get('current_heating_setpoint'),
		self.name,
		"trigger_trv_change()"
	) is None:
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
		self._last_reported_valve_position = valve_position
		self._last_reported_valve_position_update_wait_lock.release()
