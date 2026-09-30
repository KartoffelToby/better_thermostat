"""Background work the control loop starts ends when the entity goes away.

Each setpoint, mode and calibration write arms a watchdog that waits for the
device to confirm it, for up to six minutes. A watchdog that outlives an
unload or a reload keeps reading and writing a TRV on behalf of an entity that
no longer exists, next to the one a reload has just created.
"""

import asyncio
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
import pytest

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    WRITE_BUDGET,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)

_CTRL = "custom_components.better_thermostat.utils.controlling"


@pytest.mark.parametrize("removal", ["unload", "reload"])
async def test_a_pending_write_watchdog_ends_with_the_entity(hass, fake_trv, removal):
    """A watchdog still waiting for its confirmation is cancelled on removal."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    armed = asyncio.Event()

    async def watchdog_in_its_window(_entity, _entity_id, *_write):
        armed.set()
        await asyncio.Event().wait()

    with (
        patch(f"{_CTRL}.check_target_temperature", new=watchdog_in_its_window),
        patch(WRITE_BUDGET, 0.0),
    ):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 23.5},
            blocking=True,
        )
        assert await wait_for(hass, armed.is_set)
        watchdogs = [
            task
            for task in bt.task_manager.tasks
            if task.get_name().startswith("bt_check_target_temp_")
        ]
        assert watchdogs

        if removal == "unload":
            assert await hass.config_entries.async_unload(entry.entry_id)
        else:
            assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert all(task.done() for task in watchdogs)
    assert not bt.task_manager.tasks
    if removal == "unload":
        assert entry.state is ConfigEntryState.NOT_LOADED
    else:
        assert hass.data[DOMAIN][entry.entry_id]["climate"] is not bt
