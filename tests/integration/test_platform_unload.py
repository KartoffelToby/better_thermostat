"""Unloading an entry releases what its sensor platform subscribed to.

The sensor platform keeps a dispatcher subscription per entry that listens
for configuration changes and holds the climate entity it reports on. An
unloaded entry that keeps it leaks one subscription, and one dead climate
object, per reload.
"""

from homeassistant.helpers.dispatcher import DATA_DISPATCHER

from custom_components.better_thermostat.utils.const import SIGNAL_CONFIG_CHANGED

from .conftest import make_entry, set_room_sensor, setup_entry, wait_for_startup


def _config_change_listeners(hass, entry) -> int:
    """Return how many listeners the entry's configuration change signal has."""
    signal = SIGNAL_CONFIG_CHANGED.format(entry.entry_id)
    return len(hass.data.get(DATA_DISPATCHER, {}).get(signal, {}))


async def test_unload_releases_the_sensor_dispatcher_subscription(hass, fake_trv):
    """After an unload the entry holds no sensor-platform subscription."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert _config_change_listeners(hass, entry) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _config_change_listeners(hass, entry) == 0


async def test_a_reload_leaves_one_subscription_and_fresh_tracking(hass, fake_trv):
    """A reloaded entry listens once and tracks its entities anew.

    The tracked entities live on the entry's runtime data, which the reload
    replaces; the setup after it records the entities it creates again.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    tracked_before = entry.runtime_data.tracked_entities

    assert await hass.config_entries.async_reload(entry.entry_id)
    await wait_for_startup(hass, entry)

    tracked_after = entry.runtime_data.tracked_entities
    assert _config_change_listeners(hass, entry) == 1
    assert tracked_after is not tracked_before
    assert tracked_after.switches
    assert tracked_after.switches == tracked_before.switches
