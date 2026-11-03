"""A state held back for a copy of an unreadable store is saved at stop.

A stored state that could not be read in full is copied aside before
anything replaces it. When that copy fails at start, the state waits for
it; Home Assistant's final write is the last chance to take the copy and
save what was learned since, and the stored payload must stay as it was
while the copy still cannot be written.
"""

from unittest.mock import patch

from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState
from homeassistant.helpers import storage
from homeassistant.util.file import WriteError
import pytest

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup

POISON = {"version": 1, "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}


async def _stop(hass) -> None:
    """Take Home Assistant through the stop stages that concern storage."""
    hass.set_state(CoreState.stopping)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    hass.set_state(CoreState.final_write)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


@pytest.mark.parametrize("disk_recovers", [True, False])
async def test_a_copy_that_failed_at_start_is_retried_at_stop(
    hass, hass_storage, fake_trv, disk_recovers
):
    """With the disk back, copy and learned value are stored; else nothing moves."""
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
        disk["full"] = not disk_recovers
        bt.heating_power = 0.77
        bt.schedule_save_state()
        try:
            await _stop(hass)
        finally:
            hass.set_state(CoreState.running)

    if disk_recovers:
        assert hass_storage[f"{live}.corrupt"]["data"] == POISON
        assert hass_storage[live]["data"]["thermal"]["heating_power"] == 0.77
    else:
        assert hass_storage[live]["data"] == POISON
        assert not [key for key in hass_storage if ".corrupt" in key]
