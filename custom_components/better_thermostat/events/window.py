import logging
import asyncio

from ..controlling import control_trv
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

@callback
async def trigger_window_change(self):
	if self.startup_running:
		return
	if self.hass.states.get(self.heater_entity_ids) is not None:
		await asyncio.sleep(int(self.window_delay))
		check = self.hass.states.get(self.window_sensors_entity_ids).state
		if check == 'on':
			self.window_open = True
		elif check == 'off':
			self.window_open = False
		elif check == 'unknown':
			self.window_open = True
			_LOGGER.warning("better_thermostat %s: Window sensor state is unknown, assuming window is open", self.name)
		else:
			_LOGGER.error("better_thermostat %s: New window sensor state not recognized", self.name)
			return
		_LOGGER.debug("better_thermostat %s: Window (group) state changed to %s", self.name, "open" if self.window_open else "closed")
		self.async_write_ha_state()
		await control_trv(self)
