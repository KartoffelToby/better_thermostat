"""Shared contact sensor event handling and debounce queue.

Window and door sensors follow the same contract: a binary contact whose
confirmed "open" state suppresses heating after a configurable delay.
This module implements that behavior once; :mod:`events.window` and
:mod:`events.door` bind it to the respective entity attributes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from custom_components.better_thermostat import DOMAIN

_LOGGER = logging.getLogger(__name__)

_OPEN_STATES = ("on", "true", "open", "unknown", "unavailable")
_CLOSED_STATES = ("off", "false", "closed")


@dataclass(frozen=True)
class ContactRole:
    """Binding of the shared contact logic to one sensor kind.

    Attributes name the BetterThermostat instance attributes that hold the
    sensor entity id, the confirmed open state, the debounce delays and the
    event queue for this kind of contact.
    """

    kind: str
    entity_id_attr: str
    open_attr: str
    delay_attr: str
    delay_after_attr: str
    queue_attr: str
    issue_translation_key: str
    learn_more_url: str | None = None


WINDOW = ContactRole(
    kind="window",
    entity_id_attr="window_id",
    open_attr="window_open",
    delay_attr="window_delay",
    delay_after_attr="window_delay_after",
    queue_attr="window_queue_task",
    issue_translation_key="invalid_window_state",
    learn_more_url="https://better-thermostat.org/faq/window-sensor",
)

DOOR = ContactRole(
    kind="door",
    entity_id_attr="door_id",
    open_attr="door_open",
    delay_attr="door_delay",
    delay_after_attr="door_delay_after",
    queue_attr="door_queue_task",
    issue_translation_key="invalid_door_state",
)


@callback
async def trigger_contact_change(self, role: ContactRole, event) -> None:
    """Handle a contact sensor state event and queue the debounced change.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    role :
            Binding that selects the window or door attributes.
    event :
            Event object from the eventbus. Contains the new and old state
            from the contact sensor (group).

    Returns
    -------
    None
    """

    entity_id = getattr(self, role.entity_id_attr)
    new_state = event.data.get("new_state")

    if None in (self.hass.states.get(entity_id), entity_id, new_state):
        return

    new_state = new_state.state

    old_contact_open = getattr(self, role.open_attr)

    if new_state in _OPEN_STATES:
        new_contact_open = True
        if new_state == "unknown":
            _LOGGER.warning(
                "better_thermostat %s: %s sensor state is unknown, assuming %s is open",
                self.device_name,
                role.kind.capitalize(),
                role.kind,
            )
        elif new_state == "unavailable":
            _LOGGER.info(
                "better_thermostat %s: %s sensor is unavailable, assuming %s is open",
                self.device_name,
                role.kind.capitalize(),
                role.kind,
            )

        # contact was opened, disable heating power calculation for this period
        self._heating_tracker.start_temp = None
        self.async_write_ha_state()
    elif new_state in _CLOSED_STATES:
        new_contact_open = False
    else:
        _LOGGER.error(
            "better_thermostat %s: New %s sensor state '%s' not recognized",
            self.device_name,
            role.kind,
            new_state,
        )
        ir.async_create_issue(
            hass=self.hass,
            domain=DOMAIN,
            issue_id=f"{role.issue_translation_key}_{self.device_name}",
            is_fixable=False,
            is_persistent=False,
            learn_more_url=role.learn_more_url,
            severity=ir.IssueSeverity.ERROR,
            translation_key=role.issue_translation_key,
            translation_placeholders={
                "name": str(self.device_name),
                "state": str(new_state),
            },
        )
        return

    # make sure to skip events which do not change the saved contact state:
    if new_contact_open == old_contact_open:
        _LOGGER.debug(
            "better_thermostat %s: %s state did not change, skipping event",
            self.device_name,
            role.kind.capitalize(),
        )
        return
    await getattr(self, role.queue_attr).put(new_contact_open)


async def contact_queue(self, role: ContactRole):
    """Process queued contact-open events for one sensor kind.

    This coroutine dequeues contact state changes, applies configured wait
    delays and triggers the control queue when the contact remains in the
    expected state after the delay.
    """
    queue: asyncio.Queue = getattr(self, role.queue_attr)
    entity_id = getattr(self, role.entity_id_attr)
    try:
        while True:
            contact_event_to_process = await queue.get()
            try:
                if contact_event_to_process is not None:
                    if contact_event_to_process:
                        _LOGGER.debug(
                            "better_thermostat %s: %s opened, "
                            "waiting %s seconds before continuing",
                            self.device_name,
                            role.kind.capitalize(),
                            getattr(self, role.delay_attr),
                        )
                        await asyncio.sleep(getattr(self, role.delay_attr))
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: %s closed, "
                            "waiting %s seconds before continuing",
                            self.device_name,
                            role.kind.capitalize(),
                            getattr(self, role.delay_after_attr),
                        )
                        await asyncio.sleep(getattr(self, role.delay_after_attr))
                    contact_state = self.hass.states.get(entity_id)
                    if contact_state is None:
                        _LOGGER.debug(
                            "better_thermostat %s: %s sensor %s vanished "
                            "during the debounce delay; skipping event",
                            self.device_name,
                            role.kind.capitalize(),
                            entity_id,
                        )
                        continue
                    # remap the sensor state with the same vocabulary as the
                    # trigger; a state outside it cannot confirm anything
                    if contact_state.state in _OPEN_STATES:
                        current_contact_state = True
                    elif contact_state.state in _CLOSED_STATES:
                        current_contact_state = False
                    else:
                        _LOGGER.debug(
                            "better_thermostat %s: %s sensor %s reported "
                            "unrecognized state '%s' during the debounce "
                            "delay; skipping event",
                            self.device_name,
                            role.kind.capitalize(),
                            entity_id,
                            contact_state.state,
                        )
                        continue
                    # make sure the current state is the suggested change state to prevent a false positive:
                    if current_contact_state == contact_event_to_process:
                        setattr(self, role.open_attr, contact_event_to_process)
                        self.async_write_ha_state()
                        if getattr(self, "in_maintenance", False):
                            # Keep state up to date during maintenance, but defer control
                            # until maintenance ends.
                            self._control_needed_after_maintenance = True
                        else:
                            if not self.control_queue_task.empty():
                                empty_queue(self.control_queue_task)
                            await self.control_queue_task.put(self)
            except asyncio.CancelledError:
                _LOGGER.debug(
                    "better_thermostat %s: %s queue processing cancelled",
                    self.device_name,
                    role.kind.capitalize(),
                )
                raise
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        _LOGGER.debug(
            "better_thermostat %s: %s queue task cancelled",
            self.device_name,
            role.kind.capitalize(),
        )
        raise


def empty_queue(q: asyncio.Queue):
    """Empty out a Queue of pending items.

    Consumes all pending items from the queue and marks them as done.
    """
    for _ in range(q.qsize()):
        q.get_nowait()
        q.task_done()
