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
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.state_manager import StateManager

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

_SM = "custom_components.better_thermostat.utils.state_manager"

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


POISON = {"version": 1, "mpc": {"k1": {"gain_est": 0.5, "kalman_P": "NaN"}}}


async def _started_on_a_store_whose_copy_fails(hass, hass_storage, entry, disk):
    live = f"{DOMAIN}_{entry.entry_id}_state"
    hass_storage[live] = {"version": 1, "minor_version": 1, "key": live, "data": POISON}
    write = storage.Store._async_write_data

    async def _write(store, data):
        if ".corrupt" in store.key and disk["full"]:
            raise WriteError("disk full")
        await write(store, data)

    patcher = patch.object(storage.Store, "_async_write_data", _write)
    patcher.start()
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return bt, live, patcher


@pytest.mark.parametrize("disk_recovers", [True, False])
async def test_a_copy_that_failed_at_start_is_retried_at_stop(
    hass, hass_storage, fake_trv, disk_recovers
):
    """At stop the copy is tried again; the state is saved only behind it.

    With the disk back, the copy and the learned value both reach storage.
    With the disk still full, the stored payload stays as it was.
    """
    entry = make_entry()
    disk = {"full": True}
    bt, live, patcher = await _started_on_a_store_whose_copy_fails(
        hass, hass_storage, entry, disk
    )
    try:
        disk["full"] = not disk_recovers
        bt.heating_power = 0.77
        bt.schedule_save_state()
        try:
            await _stop(hass)
        finally:
            hass.set_state(CoreState.running)
    finally:
        patcher.stop()

    if disk_recovers:
        assert hass_storage[f"{live}.corrupt"]["data"] == POISON
        assert hass_storage[live]["data"]["thermal"]["heating_power"] == 0.77
    else:
        assert hass_storage[live]["data"] == POISON
        assert not [key for key in hass_storage if ".corrupt" in key]


async def test_a_copy_that_failed_at_start_is_retried_while_running(
    hass, hass_storage, fake_trv
):
    """A runtime save after the disk recovers writes the copy and the state."""
    entry = make_entry()
    disk = {"full": True}
    clock = {"now": 1000.0}
    with patch(f"{_SM}.monotonic", lambda: clock["now"]):
        bt, live, patcher = await _started_on_a_store_whose_copy_fails(
            hass, hass_storage, entry, disk
        )
        try:
            disk["full"] = False
            clock["now"] += 3600
            bt.heating_power = 0.77
            bt.schedule_save_state()
            await _let_the_save_run(hass)
        finally:
            patcher.stop()

    assert hass_storage[f"{live}.corrupt"]["data"] == POISON
    assert hass_storage[live]["data"]["thermal"]["heating_power"] == 0.77
