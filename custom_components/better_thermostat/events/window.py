import asyncio
import logging

from custom_components.better_thermostat import DOMAIN
from homeassistant.core import callback
from homeassistant.const import STATE_OFF
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_window_change(self, event) -> None:
    new_state = event.data.get("new_state")
    if not all(self.hass.states.get(sensor) for sensor in self.window_ids):
        return

    new_state = new_state.state
    old_window_open = self.window_open

    if new_state in ("on", "unknown", "unavailable"):
        new_window_open = True
        if new_state == "unknown":
            _LOGGER.warning("better_thermostat %s: Window sensor state is unknown, assuming window is open", self.device_name)
        self.heating_start_temp = None
        self.async_write_ha_state()
    elif new_state == "off":
        new_window_open = False
    else:
        _LOGGER.error(f"better_thermostat %s: New window sensor state '{new_state}' not recognized")
        return

    current_window_open = any(
        self.hass.states.get(sensor).state in ("on", "open", "true") for sensor in self.window_ids
    )

    if new_window_open == old_window_open and new_window_open == current_window_open:
        _LOGGER.debug(f"better_thermostat %s: Window state did not change, skipping event")
        return
    await self.window_queue_task.put(new_window_open)

async def window_queue(self):
    try:
        while True:
            window_event_to_process = await self.window_queue_task.get()
            try:
                if window_event_to_process is not None:
                    if window_event_to_process:
                        _LOGGER.debug(f"better_thermostat {self.device_name}: Window opened, waiting {self.window_delay} seconds before continuing")
                        await asyncio.sleep(self.window_delay)
                    else:
                        _LOGGER.debug(f"better_thermostat {self.device_name}: Window closed, waiting {self.window_delay_after} seconds before continuing")
                        await asyncio.sleep(self.window_delay_after)
                    current_window_state = any(
                        self.hass.states.get(sensor).state in ("on", "open", "true") for sensor in self.window_ids
                    )
                    if current_window_state == window_event_to_process:
                        self.window_open = window_event_to_process
                        self.async_write_ha_state()
                        if not self.control_queue_task.empty():
                            empty_queue(self.control_queue_task)
                        await self.control_queue_task.put(self)
            except asyncio.CancelledError:
                raise
            finally:
                self.window_queue_task.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(f"better_thermostat {self.device_name}: Window queue task cancelled")
        raise


def empty_queue(q: asyncio.Queue):
    for _ in range(q.qsize()):
        q.get_nowait()
        q.task_done()
