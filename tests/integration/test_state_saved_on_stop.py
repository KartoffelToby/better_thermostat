"""Learned state scheduled for saving reaches the store when Home Assistant stops.

A learned value is saved a few seconds after it changes, so that a burst of
changes costs one write. Home Assistant does not remove entities when it
stops, so a save still waiting at that point must be written on the way
down; otherwise the next start restores what was learned before it.
"""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.state_manager import StateManager

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

LEARNED = 0.5
CHANGED = 0.02


def _stored_power(hass_storage, entry) -> float | None:
    """Return the heating power the store last wrote."""
    stored = hass_storage[f"{DOMAIN}_{entry.entry_id}_state"]["data"]
    return stored["thermal"]["heating_power"]


async def _let_the_save_run(hass) -> None:
    """Move the clock past the debounce of a scheduled save."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


async def _stop(hass) -> None:
    """Take Home Assistant through the stop stages that concern storage."""
    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    hass.set_state(CoreState.final_write)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


async def _started_with_a_saved_value(hass, hass_storage, entry):
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    bt.heating_power = LEARNED
    bt.schedule_save_state()
    await _let_the_save_run(hass)
    assert _stored_power(hass_storage, entry) == LEARNED
    return bt


async def test_a_change_pending_at_stop_is_stored(hass, hass_storage, fake_trv):
    """A change still inside the save delay when Home Assistant stops is stored."""
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    bt.heating_power = CHANGED
    bt.schedule_save_state()

    try:
        await _stop(hass)
    finally:
        hass.set_state(CoreState.running)

    assert _stored_power(hass_storage, entry) == CHANGED


async def test_a_change_pending_at_removal_is_stored(hass, hass_storage, fake_trv):
    """A change still inside the save delay when the entry unloads is stored."""
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    bt.heating_power = CHANGED
    bt.schedule_save_state()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _stored_power(hass_storage, entry) == CHANGED


async def test_a_pending_change_is_saved_once(hass, hass_storage, fake_trv):
    """The save at stop replaces the pending one; the delay adds no second."""
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    bt.heating_power = CHANGED
    bt.schedule_save_state()

    with patch.object(
        StateManager, "save", autospec=True, wraps=StateManager.save
    ) as save:
        try:
            await _stop(hass)
            await _let_the_save_run(hass)
        finally:
            hass.set_state(CoreState.running)

    assert save.await_count == 1
    assert _stored_power(hass_storage, entry) == CHANGED
