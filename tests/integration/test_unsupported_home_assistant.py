"""A Home Assistant older than the integration needs sets nothing up.

``hacs.json`` keeps HACS from offering a release to an older core, but Home
Assistant itself does not check it. Without a check of its own, the climate
platform fails there while the switches and numbers load, and the thermostat
vanishes with no message that names the cause.
"""

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er, issue_registry as ir
import pytest

from custom_components.better_thermostat import UNSUPPORTED_HOME_ASSISTANT_ISSUE
from custom_components.better_thermostat.utils.const import MIN_HOME_ASSISTANT_VERSION

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

OLDER_CORE = "2026.10.4"

pytestmark = pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV], indirect=True, ids=profile_id
)


async def test_an_older_core_sets_up_nothing_and_says_why(hass, fake_trv):
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.better_thermostat.running_home_assistant_version", OLDER_CORE
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is not ConfigEntryState.LOADED
    assert hass.states.get(BT_ENTITY) is None
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, UNSUPPORTED_HOME_ASSISTANT_ISSUE)
    assert issue is not None
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_placeholders == {
        "required": MIN_HOME_ASSISTANT_VERSION,
        "running": OLDER_CORE,
    }


@pytest.mark.parametrize(
    "core", [MIN_HOME_ASSISTANT_VERSION, "2026.11.0b0", "2026.12.0.dev20261120"]
)
async def test_the_floor_and_newer_set_up_without_the_issue(hass, fake_trv, core):
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)

    with patch(
        "custom_components.better_thermostat.running_home_assistant_version", core
    ):
        await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert hass.states.get(BT_ENTITY) is not None
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, UNSUPPORTED_HOME_ASSISTANT_ISSUE)
        is None
    )
