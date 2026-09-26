"""Drive a config entry through a Home Assistant boot rather than a reload.

The shared harness sets entries up on a Home Assistant that is already
running. ``async_at_started`` then runs the thermostat's startup at once,
and it finishes before the platforms that depend on the climate entity are
built. On a real boot the order is the other way round: every platform is
set up while Home Assistant is still starting, and the startup waits for
``EVENT_HOMEASSISTANT_STARTED``. This module reproduces that order.
"""

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState

from .conftest import DOMAIN, wait_for_startup


async def set_up_during_boot(hass, entry):
    """Set ``entry`` up while Home Assistant is still starting.

    Returns the climate entity once every platform of the entry is added;
    its startup sequence has not run yet.
    """
    hass.set_state(CoreState.starting)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    bt = hass.data[DOMAIN][entry.entry_id]["climate"]
    # The state listeners are registered at the end of the startup, so their
    # absence shows the startup is still waiting for Home Assistant.
    assert bt._async_unsub_state_changed is None
    return bt


async def finish_boot(hass, entry):
    """Announce that Home Assistant has started and wait for the startup."""
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    return await wait_for_startup(hass, entry)
