"""Window event handling and debounce queue helper.

These helpers respond to window sensor events and implement debouncing and
delayed handling so that HVAC behavior uses window-open information reliably.
The window drives its own region of the control kernel; the behavior around
that region is shared with the door sensor and lives in :mod:`events.contact`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contact import WINDOW, contact_queue, trigger_contact_change

if TYPE_CHECKING:
    from homeassistant.core import Event, EventStateChangedData

    from custom_components.better_thermostat.climate import BetterThermostat

__all__ = ["trigger_window_change", "window_queue"]


async def trigger_window_change(
    self: BetterThermostat, event: Event[EventStateChangedData]
) -> None:
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
    await trigger_contact_change(self, WINDOW, event)


async def window_queue(self: BetterThermostat) -> None:
    """Process queued window-open events.

    Applies the configured debounce delays to the window region and fires
    the logbook entry and control kick once a change commits.
    """
    await contact_queue(self, WINDOW)
