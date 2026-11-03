"""Resetting the learned heating power outlives a Home Assistant restart.

The store is what a restart restores the learned values from, so a reset
that only changes the running entity is undone by the next start. What a
stop writes is read here from the storage the store flushes into.
"""

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE

from custom_components.better_thermostat.utils.const import SERVICE_RESET_HEATING_POWER

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

LEARNED = 0.5
RESET = 0.01


async def _stop(hass) -> None:
    """Run the final write Home Assistant performs on the way down."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()


def _stored_power(hass_storage, entry) -> float | None:
    """Return the heating power the store last wrote to disk."""
    stored = hass_storage[f"{DOMAIN}_{entry.entry_id}_state"]["data"]
    return stored["thermal"]["heating_power"]


async def test_a_reset_heating_power_is_what_the_next_start_restores(
    hass, hass_storage, fake_trv
):
    """After a reset and a stop, the store holds the reset value."""
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    bt.heating_power = LEARNED
    bt.schedule_save_state()
    await _stop(hass)
    assert _stored_power(hass_storage, entry) == LEARNED

    await hass.services.async_call(
        DOMAIN, SERVICE_RESET_HEATING_POWER, {"entity_id": BT_ENTITY}, blocking=True
    )
    await _stop(hass)

    assert bt.heating_power == RESET
    assert _stored_power(hass_storage, entry) == RESET
