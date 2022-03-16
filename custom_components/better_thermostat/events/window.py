import asyncio
import logging
from datetime import datetime, timedelta

from homeassistant.core import callback

from ..controlling import control_trv

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_window_change(self, event) -> None:
	"""Triggerd by window sensor event from HA to check if the window is open.

	Parameters
	----------
	self : 
		self instance of better_thermostat
	event : 
		Event object from the eventbus. Contains the new and old state from the window (group).

	Returns
	-------
	None
	"""
	
	new_state = event.data.get("new_state")
	
	if None in (self.hass.states.get(self.window_id), self.window_id, new_state):
		return
	
	new_state = new_state.state
	
	old_window_open = self.window_open
	
	if new_state in ('on', 'unknown'):
		new_window_open = True
		if new_state == 'unknown':
			_LOGGER.warning("better_thermostat %s: Window sensor state is unknown, assuming window is open", self.name)
	elif new_state == 'off':
		new_window_open = False
	else:
		_LOGGER.error(f"better_thermostat {self.name}: New window sensor state '{new_state}' not recognized")
		return
	
	# make sure to skip events which do not change the saved window state:
	if new_window_open == old_window_open:
		_LOGGER.debug(f"better_thermostat {self.name}: Window state did not change, skipping event")
		return
	
	# Get timestamp lock (or wait) 
	self._window_action_timestamp_lock.acquire()
	# Update timestamp
	self._window_action_timestamp = datetime.now()
	self._window_most_recent_action = new_window_open
	
	# Check if another coroutine is already waiting 
	if self._window_delay_lock.locked():
		self._window_action_timestamp_lock.release()
		return
	
	# Get delay lock (or wait) 
	self._window_delay_lock.acquire()
	
	while True:
		# save our timestamp
		_window_action_timestamp = self._window_action_timestamp
		# Let other coroutines update the timestamp
		self._window_action_timestamp_lock.release()
		
		# calculate the delay
		_delay = round((timedelta(seconds=self.window_delay) - (datetime.now() - _window_action_timestamp)).total_seconds())
		
		if _delay > 0:
			await asyncio.sleep(_delay)
		
		self._window_action_timestamp_lock.acquire()
		
		# Check if the timestamp hasn't changed
		if self._window_action_timestamp == _window_action_timestamp:
			break
	
	self._window_delay_lock.release()
	
	if self._window_most_recent_action != old_window_open:
		self.window_open = self._window_most_recent_action
		
		if self.window_open:
			_LOGGER.debug(f"better_thermostat {self.name}: Window was opened")
		else:
			_LOGGER.debug(f"better_thermostat {self.name}: Window was closed")
		
		self.async_write_ha_state()
		await control_trv(self)
	
	self._window_action_timestamp_lock.release()
