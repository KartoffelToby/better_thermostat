"""Door event handling and debounce queue helper.

Door sensors behave like window sensors but carry their own debounce
delays, so that briefly opened doors do not shut the heating down.
The shared implementation lives in :mod:`events.contact`.
"""

from __future__ import annotations

from homeassistant.core import callback

from .contact import DOOR, contact_queue, empty_queue, trigger_contact_change

__all__ = ["trigger_door_change", "door_queue", "empty_queue"]


@callback
async def trigger_door_change(self, event) -> None:
    """Triggered by door sensor event from HA to check if the door is open.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    event :
            Event object from the eventbus. Contains the new and old state from the door (group).

    Returns
    -------
    None
    """
    await trigger_contact_change(self, DOOR, event)


async def door_queue(self):
    """Process queued door-open events.

    This coroutine dequeues door state changes, applies configured wait
    delays and triggers the control queue when the door remains in the
    expected state after the delay.
    """
    await contact_queue(self, DOOR)
