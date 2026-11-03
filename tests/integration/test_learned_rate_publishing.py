"""The learned rates are rounded where they are published, and only there.

The thermostat state shows them on a fixed grid; the store a restart
restores them from keeps the value the trackers learned.
"""

from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

BT_ENTITY = "climate.bt_test"
POWER = 0.0123456789
LOSS = 0.0098765432


async def test_published_rates_are_rounded_and_stored_ones_are_not(
    hass, hass_storage, fake_trv
):
    """The state shows 4 and 5 decimals; the store holds the learned values."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    bt.heating_power = POWER
    bt.heat_loss_rate = LOSS
    bt.schedule_save_state()
    bt.async_write_ha_state()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()

    attributes = hass.states.get(BT_ENTITY).attributes
    assert (attributes["heating_power"], attributes["heat_loss"]) == (0.0123, 0.00988)
    thermal = hass_storage[f"{DOMAIN}_{entry.entry_id}_state"]["data"]["thermal"]
    assert (thermal["heating_power"], thermal["heat_loss_rate"]) == (POWER, LOSS)
