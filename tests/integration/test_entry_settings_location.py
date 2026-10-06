"""The settings of an entry live in its options, whichever version saved it.

An entry stored by 1.9 holds its settings in its data, at minor version 1; the
migration moves them into the options. 1.9.3 loads an entry of minor version
2 as well and, when its settings are saved there, writes them back into the
data and empties the options without changing the version. Such an entry is
moved again when it is set up here, and runs on the values saved in 1.9.3.
"""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.const import CONF_TOLERANCE

from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup

# Not the value make_entry stores, so a passing test read the saved one.
TOLERANCE = 0.7


async def test_an_entry_from_1_9_is_moved_into_the_options(hass, fake_trv):
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    settings = dict(entry.data)
    assert (entry.version, entry.minor_version) == (18, 1)

    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert (entry.version, entry.minor_version) == (18, 2)
    assert entry.data == {}
    assert entry.options == settings


async def test_an_entry_saved_again_by_1_9_3_runs_on_what_was_saved_there(
    hass, fake_trv
):
    set_room_sensor(hass, 19.0)
    saved_in_1_9_3 = dict(make_entry(fake_trv.profile).data) | {
        CONF_TOLERANCE: TOLERANCE
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=18,
        minor_version=2,
        data=saved_in_1_9_3,
        options={},
        title="BT Test",
    )

    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert bt.tolerance == TOLERANCE
    assert entry.data == {}
    assert entry.options == saved_in_1_9_3
