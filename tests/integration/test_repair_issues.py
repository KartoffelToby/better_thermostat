"""The missing-entity repair issue over the life of a config entry.

The issue names a TRV that is gone. Nothing Better Thermostat can do brings
the TRV back, so the issue offers no fix: it stays for as long as the TRV is
missing and the entry still controls it, and it goes on its own when either
stops being true. The entry's own settings can change around it, through a
rename or a reload, without leaving an issue behind that nothing clears.
"""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
import pytest

from custom_components.better_thermostat.utils.helpers import (
    entry_issue_id,
    entry_settings,
    stored_trv_configs,
)

from .conftest import (
    CRITICAL_GRACE,
    DOMAIN,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GROUP_OF_THREE

# A grace window that is already over by the time the first check runs, so a
# TRV that is missing at startup is reported without waiting minutes for it.
NO_GRACE = timedelta(seconds=0)

pytestmark = pytest.mark.parametrize(
    "trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id
)


def bt_issues(hass) -> list[str]:
    """Return the repair issues Better Thermostat currently holds open."""
    return sorted(
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    )


def missing_entity_issue(entry, entity_id: str) -> str:
    """Return the id of the repair issue that names ``entity_id`` as missing."""
    return entry_issue_id(entry.entry_id, "missing_entity", entity_id)


async def start_with_a_head_gone(hass, trv_group):
    """Start the group's room with its second head off the air.

    Returns the entry and the absent head once startup has finished and the
    absent head has been reported.
    """
    set_room_sensor(hass, 19.5)
    absent = trv_group[1]
    absent.set_available(False)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    assert await wait_for(hass, lambda: bt_issues(hass))
    return entry, absent


async def change_the_options(hass, entry, **changes) -> None:
    """Save ``changes`` into the entry's settings and let the reload finish."""
    hass.config_entries.async_update_entry(
        entry, options={**entry_settings(entry), **changes}
    )
    await hass.async_block_till_done()
    await wait_for_startup(hass, entry)


async def test_a_missing_trv_raises_an_issue_with_no_fix_and_clears_on_return(
    hass, trv_group
):
    """The issue names the missing TRV, offers nothing to confirm, and goes with it.

    Home Assistant answers a fixable issue that has no repair flow with a
    confirm dialog whose Submit deletes the issue while the TRV is still
    gone, and the thermostat, which reported the outage already, would not
    report it again.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        entry, absent = await start_with_a_head_gone(hass, trv_group)

        issue_id = missing_entity_issue(entry, absent.entity_id)
        assert bt_issues(hass) == [issue_id]
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.is_fixable is False
        assert issue.translation_key == "missing_entity"

        absent.set_available(True)
        assert await wait_for(hass, lambda: not bt_issues(hass))


async def test_removing_the_missing_trv_from_the_entry_clears_its_issue(
    hass, trv_group
):
    """Taking the missing TRV out of the settings is the fix, and it is enough.

    The entry no longer controls the TRV, so nothing is left to report about
    it, even though the TRV is still gone.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        entry, absent = await start_with_a_head_gone(hass, trv_group)
        kept = [
            trv
            for trv in stored_trv_configs(entry_settings(entry))
            if trv["trv"] != absent.entity_id
        ]

        await change_the_options(hass, entry, thermostat=kept)

    assert entry.state is ConfigEntryState.LOADED
    assert list(entry.runtime_data.climate.real_trvs) == [trv["trv"] for trv in kept]
    assert bt_issues(hass) == []


async def test_unloading_the_entry_clears_its_issues(hass, trv_group):
    """An entry that is not running reports nothing.

    Setting it up again reports the TRV that is still missing.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        entry, absent = await start_with_a_head_gone(hass, trv_group)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert bt_issues(hass) == []

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await wait_for_startup(hass, entry)
        assert await wait_for(hass, lambda: bt_issues(hass))

    assert bt_issues(hass) == [missing_entity_issue(entry, absent.entity_id)]
    assert entry.runtime_data.climate.devices_errors == [absent.entity_id]


async def test_a_trv_still_missing_after_a_reload_is_reported_again(hass, trv_group):
    """A reload that leaves the missing TRV in the entry does not lose its issue.

    The reload deletes the entry's issues with the unload, and the reloaded
    thermostat reports the TRV again once its grace window has closed.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        entry, absent = await start_with_a_head_gone(hass, trv_group)
        old_thermostat = entry.runtime_data.climate

        await change_the_options(hass, entry, tolerance=0.5)
        assert await wait_for(hass, lambda: bt_issues(hass))

    assert entry.runtime_data.climate is not old_thermostat
    assert bt_issues(hass) == [missing_entity_issue(entry, absent.entity_id)]
    assert entry.runtime_data.climate.devices_errors == [absent.entity_id]


async def test_renaming_the_thermostat_leaves_no_issue_behind(hass, trv_group):
    """A renamed thermostat still owns the issue it raised under its old name.

    The TRV that comes back after the rename clears the one issue there is,
    so none is left that names a thermostat which no longer exists.
    """
    with patch(CRITICAL_GRACE, NO_GRACE):
        entry, absent = await start_with_a_head_gone(hass, trv_group)
        issue_before = bt_issues(hass)

        await change_the_options(hass, entry, name="BT Renamed")
        assert await wait_for(hass, lambda: bt_issues(hass))
        assert entry.runtime_data.climate.device_name == "BT Renamed"
        assert bt_issues(hass) == issue_before
        issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_before[0])
        assert issue is not None
        assert issue.translation_placeholders is not None
        assert issue.translation_placeholders["name"] == "BT Renamed"

        absent.set_available(True)
        assert await wait_for(hass, lambda: not bt_issues(hass))
