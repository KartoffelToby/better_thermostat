import logging
import asyncio

from homeassistant.components.climate.const import (HVAC_MODE_OFF)
from homeassistant.core import callback
from custom_components.better_thermostat.const import ATTR_VALVE_POSITION
from custom_components.better_thermostat.models.models import convert_inbound_states

_LOGGER = logging.getLogger(__name__)

@callback
async def trigger_trv_change(self, event):
	"""Process TRV status updates"""
	if self.startup_running:
			return

	old_state = event.data.get("old_state")
	new_state = event.data.get("new_state")

	if new_state is None or old_state is None:
			return

	if new_state.attributes is not None:
			try:
					remapped_state = convert_inbound_states(self, new_state.attributes)
					
					# write valve position to local variable
					if remapped_state.get(ATTR_VALVE_POSITION) is not None:
							self._last_reported_valve_position = remapped_state.get(ATTR_VALVE_POSITION)
							self._last_reported_valve_position_update_wait_lock.release()
					
					if old_state.attributes.get('system_mode') != new_state.attributes.get('system_mode'):
							self._hvac_mode = remapped_state.get('system_mode')
							
							if self._hvac_mode != HVAC_MODE_OFF and self.window_open:
									self._hvac_mode = HVAC_MODE_OFF
									_LOGGER.debug("better_thermostat %s: Window open, turn off the heater", self.name)
									await self._async_control_heating()
					
					if not self.ignore_states and new_state.attributes.get(
									'current_heating_setpoint'
					) is not None and self._hvac_mode != HVAC_MODE_OFF and self.calibration_type == 0:
							_new_heating_setpoint = float(new_state.attributes.get('current_heating_setpoint'))
							# if new setpoint is lower than the min setpoint, set it to the min setpoint
							_overwrite_thermostat_update = False
							if _new_heating_setpoint < self._min_temp:
									_new_heating_setpoint = self._min_temp
									_overwrite_thermostat_update = True
							# if new setpoint is higher than the max setpoint, set it to the max setpoint
							if _new_heating_setpoint > self._max_temp:
									_new_heating_setpoint = self._max_temp
									_overwrite_thermostat_update = True
							if self._target_temp != _new_heating_setpoint:
									self._target_temp = _new_heating_setpoint
							else:
									_overwrite_thermostat_update = False
							
							# if the user has changed the setpoint to a value that is not in the allowed range,
							# overwrite the change with an TRV update
							if _overwrite_thermostat_update:
									_LOGGER.warning(
											"better_thermostat %s: Overwriting setpoint %s with %s, as the new setpoint is out of bound (min/max temperature)",
											self.name,
											new_state.attributes.get('current_heating_setpoint'),
											_new_heating_setpoint
									)
									await self._async_control_heating()
			
			
			except TypeError as e:
					_LOGGER.debug("better_thermostat entity not ready or device is currently not supported %s", e)
			
			self.async_write_ha_state()