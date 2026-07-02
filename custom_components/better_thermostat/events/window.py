"""Window event handling and debounce queue helper.

These helpers respond to window sensor events and implement debouncing and
delayed handling so that HVAC behavior uses window-open information reliably.
The shared implementation lives in :mod:`events.contact`.
"""

from __future__ import annotations

from homeassistant.core import callback

from .contact import WINDOW, contact_queue, empty_queue, trigger_contact_change

__all__ = ["trigger_window_change", "window_queue", "empty_queue"]


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
    await trigger_contact_change(self, WINDOW, event)


async def window_queue(self):
    """Process queued window-open events.

    This coroutine dequeues window state changes, applies configured wait
    delays and triggers the control queue when the window remains in the
    expected state after the delay.
    """
    await contact_queue(self, WINDOW)
