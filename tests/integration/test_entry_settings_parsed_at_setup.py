"""Setup parses an entry's settings once and refuses settings it cannot read.

The parsed settings ride on the entry's runtime data while the stored ones
stay as they were. An entry whose settings do not have the shape the
integration reads ends in a setup error that says why, and its options flow
still opens, because it reads the stored settings rather than the parsed ones.
"""

import copy
import importlib
from itertools import product
from pathlib import Path

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.utils.entry_schema import parse_settings

from . import device_profiles
from .conftest import DOMAIN, make_entry, set_room_sensor, setup_entry, wait_for_startup
from .device_profiles import TRV_ID, DeviceProfile, GroupScenario, RoleScenario

_SHAPES = (DeviceProfile, RoleScenario, GroupScenario)


def _every_shape_the_suite_names() -> list[
    DeviceProfile | RoleScenario | GroupScenario
]:
    """Return each module-level device shape the integration tests define."""
    modules = [device_profiles] + [
        importlib.import_module(f"{__package__}.{path.stem}")
        for path in sorted(Path(__file__).parent.glob("test_*.py"))
    ]
    found: dict[int, DeviceProfile | RoleScenario | GroupScenario] = {}
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, dict):
                candidates = list(value.values())
            elif isinstance(value, (tuple, list)):
                candidates = list(value)
            else:
                candidates = [value]
            for candidate in candidates:
                if isinstance(candidate, _SHAPES):
                    found[id(candidate)] = candidate
    return list(found.values())


def test_the_scan_finds_the_shapes_the_suite_builds_entries_for():
    """A scan that finds nothing would pass for the wrong reason."""
    shapes = _every_shape_the_suite_names()
    assert device_profiles.GENERIC_HEAT_TRV in shapes
    assert device_profiles.GROUP_OF_THREE in shapes
    assert device_profiles.DUAL_ROLE in shapes


def test_every_entry_the_shared_fixture_builds_parses():
    """``make_entry`` builds the entries the integration tests run on."""
    built = 0
    for shape in _every_shape_the_suite_names():
        for window, humidity, swapped, outdoor in product((False, True), repeat=4):
            try:
                entry = make_entry(
                    shape,
                    with_window=window,
                    with_humidity=humidity,
                    heat_auto_swapped=swapped,
                    with_outdoor_sensor=outdoor,
                )
            except ValueError:
                # make_entry refuses a group whose heads disagree on the step.
                continue
            parse_settings(entry.data)
            built += 1
    assert built


async def test_setup_puts_the_parsed_settings_on_the_runtime_data(hass, fake_trv):
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    stored = copy.deepcopy(dict(entry.data))

    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)

    assert entry.options == stored
    assert entry.runtime_data.settings == parse_settings(stored)


def _broken_entry() -> MockConfigEntry:
    """Return an entry whose thermostat carries no integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        version=18,
        minor_version=2,
        data={},
        options={
            "name": "BT Test",
            "thermostat": [{"trv": TRV_ID, "model": "Generic", "advanced": {}}],
            "temperature_sensor": "sensor.room_temperature",
        },
        title="BT Test",
    )


async def test_settings_setup_cannot_read_end_in_a_translated_setup_error(hass):
    entry = _broken_entry()
    stored = copy.deepcopy(dict(entry.options))
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "invalid_settings"
    assert entry.error_reason_translation_placeholders == {
        "reason": "thermostat[0].integration is missing"
    }
    assert entry.reason == (
        "The stored settings of this Better Thermostat cannot be read: "
        "thermostat[0].integration is missing. Change them in its options, or "
        "remove it and add it again"
    )
    assert hass.states.async_entity_ids("climate") == []
    assert entry.options == stored


async def test_the_options_flow_opens_for_an_entry_setup_refused(hass):
    entry = _broken_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.parametrize(
    "options",
    [
        {"name": "BT Test"},
        {"name": 7, "thermostat": [{"trv": TRV_ID, "integration": "test"}]},
    ],
    ids=["no_thermostat", "name_not_a_string"],
)
async def test_an_entry_missing_what_every_reader_needs_is_refused(hass, options):
    entry = MockConfigEntry(
        domain=DOMAIN, version=18, minor_version=2, options=options, title="BT"
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "invalid_settings"
