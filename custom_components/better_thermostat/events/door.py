import asyncio
import logging

from custom_components.better_thermostat import DOMAIN
from homeassistant.core import callback
from homeassistant.const import STATE_OFF
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_door_change(self, event) -> None:
    new_state = event.data.get("new_state")
    if not all(self.hass.states.get(sensor) for sensor in self.door_ids):
        return

    new_state = new_state.state

    old_door_open = self.door_open

    if new_state in ("on", "unknown", "unavailable"):
        new_door_open = True
        if new_state == "unknown":
            _LOGGER.warning(
                "better_thermostat %s: Door sensor state is unknown, assuming door is open",
                self.device_name,
            )

        # door was opened, disable heating power calculation for this period
        self.heating_start_temp = None
        self.async_write_ha_state()
    elif new_state == "off":
        new_door_open = False
    else:
        _LOGGER.error(
            f"better_thermostat {self.device_name}: New door sensor state '{new_state}' not recognized"
        )
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"missing_entity_{self.device_name}",
            issue_title=f"better_thermostat {self.device_name} has invalid door sensor state",
            issue_severity="error",
            issue_description=f"better_thermostat {self.device_name} has invalid door sensor state: {new_state}",
            issue_category="config",
            issue_suggested_action="Please check the door sensor",
        )
        return

    current_door_open = any(
        self.hass.states.get(sensor).state in ("on", "open", "true") for sensor in self.door_ids
    )

    if new_door_open == old_door_open and new_door_open == current_door_open:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: Door state did not change, skipping event"
        )
        return
    await self.door_queue_task.put(new_door_open)

async def door_queue(self):
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
                    current_door_state = any(
                        self.hass.states.get(sensor).state in ("on", "open", "true") for sensor in self.door_ids
                    )
                    if current_door_state == door_event_to_process:
                        self.door_open = door_event_to_process
                        self.async_write_ha_state()
                        if not self.control_queue_task.empty():
                            empty_queue(self.control_queue_task)
                        await self.control_queue_task.put(self)
            except asyncio.CancelledError:
                raise
            finally:
                self.door_queue_task.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: Door queue task cancelled"
        )
        raise


def empty_queue(q: asyncio.Queue):
    """Empty the given asyncio queue."""
    while not q.empty():
        q.get_nowait()
        q.task_done()
