"""Deleting a config entry leaves none of its stored state behind.

The runtime state and every copy of an unreadable one are written per
entry; nothing reads them once the entry is gone, so its removal deletes
them. Another entry's state is left alone.
"""

from unittest.mock import patch

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup
from .test_state_saved_on_stop import CHANGED, _started_with_a_saved_value


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
