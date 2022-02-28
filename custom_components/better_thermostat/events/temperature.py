import logging

from homeassistant.const import (STATE_UNAVAILABLE, STATE_UNKNOWN)
from homeassistant.core import callback

from ..controlling import control_trv

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_temperature_change(self, event):
	"""Handle temperature changes.

	Parameters
	----------
	self : 
		FIXME
	event : 
		FIXME

	Returns
	-------
	FIXME
		FIXME
	"""
	if self.startup_running:
		return
	new_state = event.data.get("new_state")
	if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
		return
	
	_async_update_temp(self, new_state)
	self.async_write_ha_state()
	await control_trv(self)


@callback
def _async_update_temp(self, state):
	"""Update thermostat with the latest state from sensor.

	Parameters
	----------
	self : 
		FIXME
	state : 
		FIXME
	
	Returns
	-------
	FIXME
		FIXME
	"""
	try:
		self._cur_temp = float(state.state)
	except (ValueError, AttributeError, KeyError, TypeError, NameError, IndexError):
		_LOGGER.error(
			"better_thermostat %s: Unable to update temperature sensor status from status update, current temperature not a number",
			self.name
		)
