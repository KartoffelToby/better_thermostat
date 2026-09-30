"""A thermostat belongs to one Better Thermostat entry only.

Two entries driving the same device write competing setpoints and modes to
it. The flows refuse to hand a device to a second entry. Entries that
already share one keep running; setup names the overlap in the log and in a
repair issue, which goes away once the overlap is gone.
"""

import json
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    build_devices,
    make_entry,
    set_room_sensor,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, SPARE_HEAT_TRV

TRV_ID = GENERIC_HEAT_TRV.entity_id
SPARE_ID = SPARE_HEAT_TRV.entity_id
CATALOGS = sorted(
    (
        Path(__file__).parents[2]
        / "custom_components"
        / "better_thermostat"
        / "translations"
    ).glob("*.json")
)


def _entry(name: str, *trv_entity_ids: str) -> MockConfigEntry:
    """Return an entry named ``name`` that controls ``trv_entity_ids``."""
    bundles = {
        TRV_ID: make_entry(GENERIC_HEAT_TRV).data[CONF_HEATER][0],
        SPARE_ID: make_entry(SPARE_HEAT_TRV).data[CONF_HEATER][0],
    }
    data = {
        **make_entry(GENERIC_HEAT_TRV).data,
        "name": name,
        CONF_HEATER: [dict(bundles[trv_entity_id]) for trv_entity_id in trv_entity_ids],
    }
    return MockConfigEntry(domain=DOMAIN, version=18, data=data, title=name)


async def _set_up(hass, *entries: MockConfigEntry) -> None:
    """Add and start ``entries``; the first setup of the domain starts them all."""
    for entry in entries:
        entry.add_to_hass(hass)
    for entry in entries:
        if entry.state is not ConfigEntryState.LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    for entry in entries:
        await wait_for_startup(hass, entry)


def _user_step(name: str, *trv_entity_ids: str) -> dict:
    return {"name": name, CONF_HEATER: list(trv_entity_ids), CONF_SENSOR: SENSOR_ID}


def _shared_issues(hass) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN and issue.translation_key == "shared_trv"
    }


def _overlap_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("custom_components.better_thermostat")
        and "also controlled by" in record.getMessage()
    ]


@pytest.fixture
async def devices(hass):
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV, SPARE_HEAT_TRV)


@pytest.mark.parametrize("name", ["Room A", "Room B"])
async def test_a_new_entry_cannot_take_a_thermostat_another_entry_controls(
    hass, devices, name
):
    """The create flow stops with a translated reason, under any name."""
    await _set_up(hass, _entry("Room A", TRV_ID))

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _user_step(name, SPARE_ID, TRV_ID)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "trv_in_use"
    assert result["description_placeholders"] == {"trv": TRV_ID, "entry": "Room A"}
    for catalog in CATALOGS:
        aborts = json.loads(catalog.read_text(encoding="utf-8"))["config"]["abort"]
        assert "{trv}" in aborts[result["reason"]], catalog.name
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_the_settings_cannot_add_a_thermostat_another_entry_controls(
    hass, devices
):
    """The options form marks the thermostat field and keeps the entry as it is."""
    room_a, room_b = _entry("Room A", TRV_ID), _entry("Room B", SPARE_ID)
    await _set_up(hass, room_a, room_b)

    result = await hass.config_entries.options.async_init(room_b.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _user_step("Room B", SPARE_ID, TRV_ID)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_HEATER: "trv_in_use"}
    assert result["description_placeholders"]["trv"] == TRV_ID
    assert result["description_placeholders"]["entry"] == "Room A"
    assert [bundle["trv"] for bundle in room_b.data[CONF_HEATER]] == [SPARE_ID]


async def test_the_settings_of_an_entry_that_already_shares_still_save(hass, devices):
    """An overlap that exists already does not lock its entries' settings."""
    room_a, room_b = _entry("Room A", TRV_ID), _entry("Room B", TRV_ID, SPARE_ID)
    await _set_up(hass, room_a, room_b)

    result = await hass.config_entries.options.async_init(room_b.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _user_step("Room B", TRV_ID, SPARE_ID)
    )

    assert result["step_id"] == "advanced", result


async def test_an_existing_overlap_is_named_at_setup(hass, devices, caplog):
    """Both entries run; setup warns once and a repair issue names the overlap."""
    caplog.set_level(logging.INFO)
    room_a, room_b = _entry("Room A", TRV_ID), _entry("Room B", TRV_ID, SPARE_ID)
    await _set_up(hass, room_a)
    before_b = len(_overlap_warnings(caplog))

    await _set_up(hass, room_b)

    warnings = _overlap_warnings(caplog)[before_b:]
    assert len(warnings) == 1, warnings
    assert all(part in warnings[0] for part in ("Room A", "Room B", TRV_ID))
    assert SPARE_ID not in warnings[0]
    issues = _shared_issues(hass)
    assert list(issues) == [f"shared_trv_{TRV_ID}"]
    placeholders = issues[f"shared_trv_{TRV_ID}"].translation_placeholders
    assert placeholders["trv"] == TRV_ID
    assert "Room A" in placeholders["entries"]
    assert "Room B" in placeholders["entries"]
    for catalog in CATALOGS:
        issue_text = json.loads(catalog.read_text(encoding="utf-8"))["issues"]
        assert "shared_trv" in issue_text, catalog.name


async def test_the_overlap_issue_goes_when_an_entry_is_removed(hass, devices):
    """Removing one of the sharing entries clears the issue."""
    room_a, room_b = _entry("Room A", TRV_ID), _entry("Room B", TRV_ID, SPARE_ID)
    await _set_up(hass, room_a, room_b)
    assert _shared_issues(hass)

    await hass.config_entries.async_remove(room_a.entry_id)
    await hass.async_block_till_done()

    assert _shared_issues(hass) == {}


async def test_the_overlap_issue_goes_when_the_settings_drop_the_thermostat(
    hass, devices, caplog
):
    """Taking the shared thermostat out of one entry clears the issue."""
    room_a, room_b = _entry("Room A", TRV_ID), _entry("Room B", TRV_ID, SPARE_ID)
    await _set_up(hass, room_a, room_b)
    assert _shared_issues(hass)

    result = await hass.config_entries.options.async_init(room_b.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _user_step("Room B", SPARE_ID)
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await wait_for_startup(hass, room_b)

    assert _shared_issues(hass) == {}


async def test_the_overlap_issue_names_only_the_entries_that_remain(hass, devices):
    """Removing one of three sharing entries leaves an issue about the other two."""
    room_a, room_b, room_c = (
        _entry("Room A", TRV_ID),
        _entry("Room B", TRV_ID),
        _entry("Room C", TRV_ID),
    )
    await _set_up(hass, room_a, room_b, room_c)

    await hass.config_entries.async_remove(room_c.entry_id)
    await hass.async_block_till_done()

    issues = _shared_issues(hass)
    assert list(issues) == [f"shared_trv_{TRV_ID}"]
    entries = issues[f"shared_trv_{TRV_ID}"].translation_placeholders["entries"]
    assert "Room A" in entries
    assert "Room B" in entries
    assert "Room C" not in entries

    await hass.config_entries.async_remove(room_b.entry_id)
    await hass.async_block_till_done()

    assert _shared_issues(hass) == {}
