"""Deleting a config entry leaves none of its stored state behind.

The runtime state and every copy of an unreadable one are written per
entry; nothing reads them once the entry is gone, so its removal deletes
them. Another entry's state is left alone.
"""

import asyncio
from unittest.mock import patch

from homeassistant.helpers import storage

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup
from .test_state_saved_on_stop import (
    CHANGED,
    _started_on_a_store_whose_copy_fails,
    _started_with_a_saved_value,
)


def _stored_keys(hass_storage, entry) -> list[str]:
    """Return every storage key that belongs to *entry*."""
    return sorted(key for key in hass_storage if entry.entry_id in key)


def _seed(hass_storage, key: str, data: dict) -> None:
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": data}


async def test_removal_deletes_the_state_and_every_copy(hass, hass_storage, fake_trv):
    """The live store and all set-aside copies go; another entry's store stays."""
    entry = make_entry()
    await _started_with_a_saved_value(hass, hass_storage, entry)
    live = f"{DOMAIN}_{entry.entry_id}_state"
    for suffix in (".corrupt", ".corrupt.1", ".corrupt.2"):
        _seed(hass_storage, f"{live}{suffix}", {"version": "1"})
    other = f"{DOMAIN}_other_entry_state"
    _seed(hass_storage, other, {"version": 1})
    _seed(hass_storage, f"{other}.corrupt", {"version": "1"})

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _stored_keys(hass_storage, entry) == []
    assert other in hass_storage
    assert f"{other}.corrupt" in hass_storage


async def test_removal_of_an_entry_that_never_saved(hass, hass_storage, fake_trv):
    """An entry without stored state is removed without an error."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    hass_storage.pop(f"{DOMAIN}_{entry.entry_id}_state", None)

    result = await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert result == {"require_restart": False}
    assert _stored_keys(hass_storage, entry) == []


async def test_a_save_pending_at_removal_does_not_bring_the_state_back(
    hass, hass_storage, fake_trv
):
    """A change still waiting to be saved does not recreate a removed store."""
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    bt.heating_power = CHANGED
    bt.schedule_save_state()

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _stored_keys(hass_storage, entry) == []


async def test_a_failed_removal_does_not_block_the_entry_removal(
    hass, hass_storage, fake_trv, caplog
):
    """A storage error while deleting the state is logged; the entry still goes."""
    entry = make_entry()
    await _started_with_a_saved_value(hass, hass_storage, entry)

    with patch(
        "custom_components.better_thermostat.async_remove_stores",
        side_effect=OSError("read-only file system"),
    ):
        result = await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert result == {"require_restart": False}
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert "failed to remove state store" in caplog.text


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
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    bt.heating_power = CHANGED
    bt.schedule_save_state()

    await _remove_while_the_final_write_is_held(hass, entry)

    assert _stored_keys(hass_storage, entry) == []


async def test_removal_waits_for_the_final_copy(hass, hass_storage, fake_trv):
    """A copy the final save is still writing at removal is not left behind."""
    entry = make_entry()
    disk = {"full": True}
    bt, _live, patcher = await _started_on_a_store_whose_copy_fails(
        hass, hass_storage, entry, disk
    )
    try:
        disk["full"] = False
        bt.heating_power = CHANGED
        bt.state_mgr.mark_dirty()

        await _remove_while_the_final_write_is_held(hass, entry)
    finally:
        patcher.stop()

    assert _stored_keys(hass_storage, entry) == []


class _HeldWrites:
    """Hold the state writes of one entry until they are released.

    Each write waits on a gate of its own, in the order the writes start,
    so a test can let one write land while the next one is still held.
    Once :meth:`stop_holding` ran, later writes are no longer held, and
    :meth:`release_all` lets the held ones land.
    """

    def __init__(self, entry) -> None:
        self._entry_id = entry.entry_id
        self.gates: list[asyncio.Event] = []
        self._holding = True
        self._write = storage.Store._async_write_data

    def patch(self):
        held = self

        async def _held_write(store, data):
            if held._entry_id in store.key and held._holding:
                gate = asyncio.Event()
                held.gates.append(gate)
                await gate.wait()
            await held._write(store, data)

        return patch.object(storage.Store, "_async_write_data", _held_write)

    async def wait_for_write(self, count: int) -> None:
        """Wait until *count* writes are held."""
        for _ in range(200):
            if len(self.gates) >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"{count} writes expected, {len(self.gates)} held")

    def stop_holding(self) -> None:
        self._holding = False

    def release_all(self) -> None:
        self.stop_holding()
        for gate in self.gates:
            gate.set()


async def _let_the_loop_run(rounds: int = 50) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def test_removal_waits_for_a_runtime_save_under_way(hass, hass_storage, fake_trv):
    """A runtime save still writing when the entry is removed is not left behind.

    The final save writes through the same Store, whose write lock puts it
    behind the runtime write, so the removal that waits for the final save
    also follows the runtime write.
    """
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    held = _HeldWrites(entry)
    with held.patch():
        bt.heating_power = CHANGED
        bt.state_mgr.mark_dirty()
        runtime_save = hass.async_create_task(bt.state_mgr.save_if_dirty())
        await held.wait_for_write(1)
        held.stop_holding()

        removal = hass.async_create_task(
            hass.config_entries.async_remove(entry.entry_id)
        )
        await asyncio.wait({removal}, timeout=1)
        held.release_all()
        await removal
        await runtime_save
        await hass.async_block_till_done()

    assert _stored_keys(hass_storage, entry) == []


async def test_removal_waits_for_a_second_runtime_save_under_way(
    hass, hass_storage, fake_trv
):
    """A save queued behind another one does not land after the removal.

    The first save clears the unsaved mark once it lands, while the second
    one is still writing. The removal marks the state unsaved again, so the
    final save writes through the same Store and lands after the second.
    """
    entry = make_entry()
    bt = await _started_with_a_saved_value(hass, hass_storage, entry)
    held = _HeldWrites(entry)
    with held.patch():
        bt.heating_power = CHANGED
        bt.state_mgr.mark_dirty()
        first = hass.async_create_task(bt.state_mgr.save_if_dirty())
        await held.wait_for_write(1)
        second = hass.async_create_task(bt.state_mgr.save_if_dirty())
        await _let_the_loop_run()
        held.gates[0].set()
        await first
        await held.wait_for_write(2)
        assert bt.state_mgr.dirty is False
        held.stop_holding()

        removal = hass.async_create_task(
            hass.config_entries.async_remove(entry.entry_id)
        )
        await asyncio.wait({removal}, timeout=1)
        held.release_all()
        await removal
        await second
        await hass.async_block_till_done()

    assert _stored_keys(hass_storage, entry) == []
