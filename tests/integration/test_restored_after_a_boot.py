"""A thermostat booted with Home Assistant comes up with its saved settings.

Home Assistant writes its restore cache once as soon as it starts, before
the thermostat's startup runs. That write drops the cached state of every
entity that already publishes one, so a thermostat that read its saved
state only in the startup would come up on its defaults after every boot.
"""

from homeassistant.components.climate.const import HVACMode
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, State
from homeassistant.helpers import restore_state
from pytest_homeassistant_custom_component.common import mock_restore_cache

from .conftest import SENSOR_ID, make_entry, wait_for_startup

BT_ENTITY = "climate.bt_test"


async def test_the_saved_target_survives_the_restore_write_at_start(hass, fake_trv):
    """The saved target and mode are the ones the thermostat comes up with."""
    mock_restore_cache(hass, [State(BT_ENTITY, HVACMode.OFF, {"temperature": 23.5})])
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    hass.set_state(CoreState.starting)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    restore_cache = restore_state.async_get(hass)
    await restore_cache.async_dump_states()
    # Home Assistant drops the saved state in that write from 2026.10 on;
    # dropping it here holds earlier versions to the same boot.
    restore_cache.last_states.pop(BT_ENTITY, None)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)

    assert bt.bt_target_temp == 23.5
    assert bt.hvac_mode == HVACMode.OFF
