"""Unloading an entry releases what its sensor platform subscribed to.

The sensor platform keeps a dispatcher subscription per entry that listens
for configuration changes and holds the climate entity it reports on. An
unloaded entry that keeps it leaks one subscription, and one dead climate
object, per reload.
"""

from custom_components.better_thermostat import sensor

from .conftest import make_entry, set_room_sensor, setup_entry, wait_for_startup


async def test_unload_releases_the_sensor_dispatcher_subscription(hass, fake_trv):
    """After an unload the entry holds no sensor-platform subscription."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert entry.entry_id in sensor._DISPATCHER_UNSUBSCRIBES

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in sensor._DISPATCHER_UNSUBSCRIBES
