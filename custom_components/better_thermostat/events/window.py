import asyncio
import logging

from homeassistant.core import callback
from homeassistant.const import STATE_OFF

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_window_change(self, event) -> None:
    """Triggered by window sensor event from HA to check if the window is open.

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

    if new_state in ("on", "unknown"):
        new_window_open = True
        if new_state == "unknown":
            _LOGGER.warning(
                "better_thermostat %s: Window sensor state is unknown, assuming window is open",
                self.name,
            )
    elif new_state == "off":
        new_window_open = False
    else:
        _LOGGER.error(
            f"better_thermostat {self.name}: New window sensor state '{new_state}' not recognized"
        )
        return

    # make sure to skip events which do not change the saved window state:
    if new_window_open == old_window_open:
        _LOGGER.debug(
            f"better_thermostat {self.name}: Window state did not change, skipping event"
        )
        return
    await self.window_queue_task.put(new_window_open)


async def window_queue(self):
    while True:
        window_event_to_process = await self.window_queue_task.get()
        if window_event_to_process is not None:
            await asyncio.sleep(self.window_delay)
            # remap off on to true false
            current_window_state = True
            if self.hass.states.get(self.window_id).state == STATE_OFF:
                current_window_state = False
            # make sure the current state is the suggested change state to prevent a false positive:
            if current_window_state == window_event_to_process:
                self.window_open = window_event_to_process
                self.async_write_ha_state()
                await self.control_queue_task.put(self)
        self.window_queue_task.task_done()
