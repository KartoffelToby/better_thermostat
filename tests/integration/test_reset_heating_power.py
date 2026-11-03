"""Resetting the learned heating power outlives a Home Assistant restart.

The store is what a restart restores the learned values from, so a reset
that only changes the running entity is undone by the next start. What the
thermostat saves is read here from the storage the store writes into.
"""

from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.const import SERVICE_RESET_HEATING_POWER

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

BT_ENTITY = "climate.bt_test"
LEARNED = 0.5
RESET = 0.01


async def _let_the_save_run(hass) -> None:
    """Move the clock past the debounce of a scheduled save."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()


def _stored_power(hass_storage, entry) -> float | None:
    """Return the heating power the store last wrote."""
    stored = hass_storage[f"{DOMAIN}_{entry.entry_id}_state"]["data"]
    return stored["thermal"]["heating_power"]


async def test_a_reset_heating_power_is_what_the_next_start_restores(
    hass, hass_storage, fake_trv
):
    """After a reset, the store holds the reset value."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    bt.heating_power = LEARNED
    bt.schedule_save_state()
    await _let_the_save_run(hass)
    assert _stored_power(hass_storage, entry) == LEARNED

    await hass.services.async_call(
        DOMAIN, SERVICE_RESET_HEATING_POWER, {"entity_id": BT_ENTITY}, blocking=True
    )
    await _let_the_save_run(hass)

    assert bt.heating_power == RESET
    assert _stored_power(hass_storage, entry) == RESET
