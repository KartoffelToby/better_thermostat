import logging
import asyncio

from homeassistant.core import callback
from homeassistant.components.climate.const import (HVAC_MODE_OFF)

_LOGGER = logging.getLogger(__name__)

async def check_window_state(self):
	# window open detection and weather detection force turn TRV off
	if self.window_open and not self.closed_window_triggered:
		self.last_change = self._hvac_mode
		self._hvac_mode = HVAC_MODE_OFF
		self.closed_window_triggered = True
	elif not self.window_open and self.closed_window_triggered:
		self._hvac_mode = self.last_change
		self.closed_window_triggered = False

@callback
async def trigger_window_change(self, event):
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
		await self._async_control_heating()