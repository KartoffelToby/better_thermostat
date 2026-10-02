"""A diagnostics download carries what a bug report needs, and nothing secret.

The download is what users attach to a public issue. It has to say which
versions ran, what the thermostat itself reported, which device each valve
is, and what every configured sensor read. Attributes an integration chose
to publish on its climate entity can hold hardware addresses; those are
redacted.
"""

import json

from homeassistant.const import __version__ as ha_version
from homeassistant.helpers.json import JSONEncoder
import pytest

from custom_components.better_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_thermostat.utils.const import VERSION

from .conftest import (
    HUMIDITY_ID,
    OUTDOOR_ID,
    make_entry,
    profile_id,
    set_room_humidity,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import ZHA_VALVE_QUIRK_TRV


@pytest.mark.parametrize(
    "fake_trv", [ZHA_VALVE_QUIRK_TRV], indirect=True, ids=profile_id
)
async def test_the_download_carries_what_a_bug_report_needs(hass, fake_trv):
    """Versions, the thermostat's state, the valve's device and every sensor."""
    set_room_sensor(hass, 18.0)
    set_room_humidity(hass, 55.0)
    hass.states.async_set(OUTDOOR_ID, "4.0", {"unit_of_measurement": "°C"})
    entry = make_entry(fake_trv.profile, with_humidity=True, with_outdoor_sensor=True)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    entity_id = fake_trv.entity_id
    reported = hass.states.get(entity_id)
    hass.states.async_set(
        entity_id,
        reported.state,
        {**reported.attributes, "ieee": "00:11:22:33:44:55:66:77"},
    )

    download = await async_get_config_entry_diagnostics(hass, entry)

    assert download["versions"] == {
        "better_thermostat": VERSION,
        "home_assistant": ha_version,
    }
    assert download["climate"]["entity_id"] == bt.entity_id
    assert "control_mode" in download["climate"]["attributes"]
    valve = download["thermostat"][entity_id]
    assert valve["device"]["model"] == "TRVZB"
    assert valve["device"]["integration"] == "test"
    assert valve["attributes"]["ieee"] == "**REDACTED**"
    assert download["sensors"]["humidity_sensor"]["state"] == "55.0"
    assert download["sensors"]["outdoor_sensor"]["state"] == "4.0"
    assert download["sensors"]["humidity_sensor"]["entity_id"] == HUMIDITY_ID
    json.dumps(download, cls=JSONEncoder)
