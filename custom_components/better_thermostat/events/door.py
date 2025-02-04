import asyncio
import logging

from custom_components.better_thermostat import DOMAIN
from homeassistant.core import callback
from homeassistant.const import STATE_OFF
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_door_change(self, event) -> None:
    """Triggered by door sensor event from HA to check if any door is open.

    Parameters
    ----------
    self :
            Instance of BetterThermostat.
    event :
            Event object from the event bus containing the new and old state of the door sensor.

    Returns
    -------
    None
    """
    new_state = event.data.get("new_state")

    # Validate required data
    if None in (self.hass, self.door_id, new_state):
        _LOGGER.warning("Missing required data: hass, door_id, or new_state is None")
        return

    old_door_open = self.door_open
    new_door_open = False

    # Log the current door_id list
    _LOGGER.debug(f"Current door_id list: {self.door_id}")

    # Check all sensors in the list
    for sensor in self.door_id:
        _LOGGER.debug(f"Checking sensor: {sensor}")
        sensor_state = self.hass.states.get(sensor)
        if sensor_state is None:
            _LOGGER.warning(
                "better_thermostat %s: Door sensor %s state is None, assuming door is open",
                self.device_name,
                sensor,
            )
            new_door_open = True
            break
        elif sensor_state.state in ("unknown", "unavailable"):
            _LOGGER.warning(
                "better_thermostat %s: Door sensor %s state is %s, assuming door is open",
                self.device_name,
                sensor,
                sensor_state.state
            )
            new_door_open = True
            break
        elif sensor_state.state == "on":
            new_door_open = True
            break
        else:
            _LOGGER.debug(
                "better_thermostat %s: Door sensor %s state is %s",
                self.device_name,
                sensor,
                sensor_state.state
            )

    # Skip events that do not change the door state
    if new_door_open == old_door_open:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: Door state did not change, skipping event"
        )
        return

    # Process state change
    await self.door_queue_task.put(new_door_open)


async def door_queue(self):
    """Process door sensor changes using a queue to handle state transitions."""
    try:
        while True:
            door_event_to_process = await self.door_queue_task.get()
            try:
                if door_event_to_process is not None:
                    if door_event_to_process:
                        _LOGGER.debug(
                            f"better_thermostat {self.device_name}: Door opened, waiting {self.door_delay} seconds before continuing"
                        )
                        await asyncio.sleep(self.door_delay)
                    else:
                        _LOGGER.debug(
                            f"better_thermostat {self.device_name}: Door closed, waiting {self.door_delay_after} seconds before continuing"
                        )
                        await asyncio.sleep(self.door_delay_after)

                    # Determine the current state of all sensors
                    current_door_state = any(
                        self.hass.states.get(sensor) is not None and self.hass.states.get(sensor).state == "on"
                        for sensor in self.door_id
                    )
                    _LOGGER.debug(f"Current door state: {current_door_state}")

                    # Apply the new state if it matches the event
                    if current_door_state == door_event_to_process:
                        self.door_open = door_event_to_process
                        self.async_write_ha_state()
                        if not self.control_queue_task.empty():
                            empty_queue(self.control_queue_task)
                        await self.control_queue_task.put(self)
            except asyncio.CancelledError:
                _LOGGER.debug(
                    f"better_thermostat {self.device_name}: Door queue task cancelled"
                )
                raise
            except Exception as e:
                _LOGGER.error(f"better_thermostat {self.device_name}: Error processing door event: {e}")
            finally:
                self.door_queue_task.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: Door queue task cancelled"
        )
        raise
    except Exception as e:
        _LOGGER.error(f"better_thermostat {self.device_name}: Error in door_queue: {e}")


def empty_queue(q: asyncio.Queue):
    """Empty the given asyncio queue."""
    while not q.empty():
        q.get_nowait()
        q.task_done()
