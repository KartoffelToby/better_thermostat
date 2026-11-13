"""Door event handling and debounce queue helper.

Door sensors behave like window sensors but carry their own debounce
delays, so that briefly opened doors do not shut the heating down. The
door drives its own region of the control kernel, independent of the
window region; the behavior around both regions is shared and lives in
:mod:`events.contact`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contact import DOOR, contact_queue, trigger_contact_change

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData

    from custom_components.better_thermostat.climate import BetterThermostat

__all__ = ["trigger_door_change", "door_queue"]


async def trigger_door_change(
    self: BetterThermostat, event: Event[EventStateChangedData]
) -> None:
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


async def door_queue(self: BetterThermostat) -> None:
    """Process queued door-open events.

    Applies the configured debounce delays to the door region and fires the
    logbook entry and control kick once a change commits.
    """
    await contact_queue(self, DOOR)
