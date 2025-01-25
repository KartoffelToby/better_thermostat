import asyncio
import logging

from custom_components.better_thermostat import DOMAIN
from homeassistant.core import callback
from homeassistant.const import STATE_OFF
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_window_change(self, event) -> None:
    """Triggered by window sensor event from HA to check if any window is open.

    Parameters
    ----------
    self :
            Instance of BetterThermostat.
    event :
            Event object from the event bus containing the new and old state of the window sensor.

    Returns
    -------
    None
    """
    new_state = event.data.get("new_state")

    # Validate required data
    if None in (self.hass, self.window_id, new_state):
        return

    old_window_open = self.window_open
    new_window_open = False

    # Check all sensors in the list
    for sensor in self.window_id:
        sensor_state = self.hass.states.get(sensor)
        if sensor_state is None or sensor_state.state in ("unknown", "unavailable"):
            _LOGGER.warning(
                "better_thermostat %s: Window sensor %s state is unknown or unavailable, assuming window is open",
                self.device_name,
                sensor,
            )
            new_window_open = True
            break
        elif sensor_state.state == "on":
            new_window_open = True
            break

    # Skip events that do not change the window state
    if new_window_open == old_window_open:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: Window state did not change, skipping event"
        )
        return

    # Process state change
    await self.window_queue_task.put(new_window_open)


async def window_queue(self):
    """Process window sensor changes using a queue to handle state transitions."""
    try:
        while True:
            window_event_to_process = await self.window_queue_task.get()
            try:
                if window_event_to_process is not None:
                    if window_event_to_process:
                        _LOGGER.debug(
                            f"better_thermostat {self.device_name}: Window opened, waiting {self.window_delay} seconds before continuing"
                        )
                        await asyncio.sleep(self.window_delay)
                    else:
                        _LOGGER.debug(
                            f"better_thermostat {self.device_name}: Window closed, waiting {self.window_delay_after} seconds before continuing"
                        )
                        await asyncio.sleep(self.window_delay_after)

                    # Determine the current state of all sensors
                    current_window_state = any(
                        self.hass.states.get(sensor).state == "on"
                        for sensor in self.window_id
                    )

                    # Apply the new state if it matches the event
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
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: Window queue task cancelled"
        )
        raise


def empty_queue(q: asyncio.Queue):
    """Empty the given asyncio queue."""
    for _ in range(q.qsize()):
        q.get_nowait()
        q.task_done()
