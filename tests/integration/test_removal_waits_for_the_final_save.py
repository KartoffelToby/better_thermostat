"""Removing a config entry waits for the entity's final state write.

The entity's last save starts when it is removed, and removing the entry
deletes its stores after the unload. A save, or a copy of an unreadable
store, still writing at that point would recreate a store nothing reads
any more, so the unload finishes only after it.
"""

import asyncio
from unittest.mock import patch

from homeassistant.helpers import storage
from homeassistant.util.file import WriteError

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup
from .test_state_saved_on_stop import POISON


def _stored_keys(hass_storage, entry) -> list[str]:
    """Return every storage key that belongs to *entry*."""
    return sorted(key for key in hass_storage if entry.entry_id in key)


async def _remove_while_the_final_write_is_held(hass, entry) -> None:
    """Remove *entry* while its final state write waits on a gate.

    The gate opens only once the removal has had every chance to finish
    without the write, so a removal that does not wait for the write
    deletes the stores before it lands.
    """
    entered = asyncio.Event()
    release = asyncio.Event()
    write = storage.Store._async_write_data

    async def _held_write(store, data):
        if entry.entry_id in store.key:
            entered.set()
            await release.wait()
        await write(store, data)

    with patch.object(storage.Store, "_async_write_data", _held_write):
        removal = hass.async_create_task(
            hass.config_entries.async_remove(entry.entry_id)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        for _ in range(20):
            await asyncio.sleep(0)
        release.set()
        await removal
        await hass.async_block_till_done()


async def test_removal_waits_for_the_final_save(hass, hass_storage, fake_trv):
    """A final save still writing when the entry is removed is not left behind."""
    entry = make_entry(fake_trv.profile)
    set_room_sensor(hass, 19.0)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.state_mgr is not None
    bt.heating_power = 0.77
    bt.state_mgr.mark_dirty()

    await _remove_while_the_final_write_is_held(hass, entry)

    assert _stored_keys(hass_storage, entry) == []


async def test_removal_waits_for_the_final_copy(hass, hass_storage, fake_trv):
    """A copy the final save is still writing at removal is not left behind."""
    entry = make_entry(fake_trv.profile)
    live = f"{DOMAIN}_{entry.entry_id}_state"
    hass_storage[live] = {"version": 1, "minor_version": 1, "key": live, "data": POISON}
    write = storage.Store._async_write_data
    disk = {"full": True}

    async def _write(store, data):
        if ".corrupt" in store.key and disk["full"]:
            raise WriteError("disk full")
        await write(store, data)

    with patch.object(storage.Store, "_async_write_data", _write):
        set_room_sensor(hass, 19.0)
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        assert bt.state_mgr is not None
        assert bt.state_mgr.copy_pending
        disk["full"] = False
        bt.heating_power = 0.77
        bt.state_mgr.mark_dirty()

        await _remove_while_the_final_write_is_held(hass, entry)

    assert _stored_keys(hass_storage, entry) == []
