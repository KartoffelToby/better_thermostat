import logging
import asyncio

from ..controlling import control_trv
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

@callback
async def trigger_window_change(self):
	if self.startup_running:
		return
	if self.hass.states.get(self.heater_entity_id) is not None:
		await asyncio.sleep(int(self.window_delay))
		check = self.hass.states.get(self.window_sensors_entity_ids).state
		if check == 'on':
			self.window_open = True
		else:
			self.window_open = False
		_LOGGER.debug("better_thermostat %s: Window (group) state changed to %s", self.name, "open" if self.window_open else "closed")
		self.async_write_ha_state()
		await control_trv(self)
