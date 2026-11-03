"""Tests for the readers that look at any entry's stored settings.

These readers run over every Better Thermostat entry, loaded or not, and
over entries whose settings setup refuses to parse. They read the stored
settings as they are and never raise on an unexpected value.
"""

from __future__ import annotations

from homeassistant.const import CONF_NAME
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import (
    DOMAIN,
    other_entries_controlling,
    trv_entity_ids,
)
from custom_components.better_thermostat.utils.const import (
    CONF_HUMIDITY_SENSOR,
    CONF_THERMOSTAT,
)
from custom_components.better_thermostat.utils.helpers import (
    entry_name,
    entry_settings,
    setting_str,
    stored_trv_configs,
)


def _entry(
    data: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
    title: str = "Entry title",
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, data=data or {}, options=options or {}, title=title
    )


def test_entry_settings_reads_the_options_over_the_data():
    entry = _entry(
        data={CONF_NAME: "From data", "only_data": 1},
        options={CONF_NAME: "From options"},
    )

    assert entry_settings(entry) == {CONF_NAME: "From options", "only_data": 1}


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param({CONF_HUMIDITY_SENSOR: "sensor.h"}, "sensor.h", id="string"),
        pytest.param({CONF_HUMIDITY_SENSOR: ""}, "", id="empty-string"),
        pytest.param({}, None, id="missing"),
        pytest.param({CONF_HUMIDITY_SENSOR: None}, None, id="none"),
        pytest.param({CONF_HUMIDITY_SENSOR: 3}, None, id="number"),
        pytest.param({CONF_HUMIDITY_SENSOR: ["sensor.h"]}, None, id="list"),
    ],
)
def test_setting_str_reads_only_a_string(settings, expected):
    assert setting_str(settings, CONF_HUMIDITY_SENSOR) == expected


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param({CONF_NAME: "Living room"}, "Living room", id="stored-name"),
        pytest.param({CONF_NAME: ""}, "", id="empty-name"),
        pytest.param({}, "Entry title", id="no-name"),
        pytest.param({CONF_NAME: None}, "Entry title", id="name-none"),
        pytest.param({CONF_NAME: 42}, "Entry title", id="name-number"),
    ],
)
def test_entry_name_falls_back_to_the_title(settings, expected):
    assert entry_name(_entry(options=settings)) == expected


def test_entry_name_reads_a_name_kept_in_the_data():
    assert entry_name(_entry(data={CONF_NAME: "From 1.9"})) == "From 1.9"


@pytest.mark.parametrize(
    "heaters",
    [
        pytest.param(None, id="none"),
        pytest.param(3, id="number"),
        pytest.param("climate.trv", id="bare-string"),
        pytest.param({"trv": "climate.trv"}, id="mapping"),
    ],
)
def test_stored_trv_configs_reads_no_list_as_none(heaters):
    assert stored_trv_configs({CONF_THERMOSTAT: heaters}) == []


def test_stored_trv_configs_without_the_key_is_empty():
    assert stored_trv_configs({}) == []


def test_stored_trv_configs_skips_elements_that_are_no_mapping():
    first = {"trv": "climate.a"}
    second = {"integration": "mqtt"}

    configs = stored_trv_configs(
        {CONF_THERMOSTAT: [first, "climate.b", None, 3, second]}
    )

    assert configs == [first, second]


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        pytest.param({}, [], id="no-key"),
        pytest.param({CONF_THERMOSTAT: None}, [], id="none"),
        pytest.param({CONF_THERMOSTAT: 3}, [], id="number"),
        pytest.param({CONF_THERMOSTAT: True}, [], id="bool"),
        pytest.param(
            {CONF_THERMOSTAT: "climate.bare"}, ["climate.bare"], id="bare-string"
        ),
        pytest.param({CONF_THERMOSTAT: {"trv": "climate.a"}}, [], id="mapping"),
        pytest.param(
            {
                CONF_THERMOSTAT: [
                    {"trv": "climate.a", "integration": "mqtt"},
                    {"trv": ""},
                    {"trv": None},
                    {"trv": 7},
                    {"integration": "mqtt"},
                    "climate.b",
                    {"trv": "climate.c"},
                ]
            },
            ["climate.a", "climate.c"],
            id="mixed-list",
        ),
    ],
)
def test_trv_entity_ids_tolerates_every_stored_shape(settings, expected):
    assert trv_entity_ids(_entry(options=settings)) == expected


async def test_an_entry_with_a_broken_thermostat_list_owns_no_thermostat(hass):
    """An entry whose thermostat list does not parse is skipped as an owner."""
    owner = _entry(options={CONF_THERMOSTAT: [{"trv": "climate.trv"}]})
    broken = _entry(options={CONF_THERMOSTAT: 3})
    empty = _entry()
    for entry in (owner, broken, empty):
        entry.add_to_hass(hass)

    assert other_entries_controlling(hass, "climate.trv", None) == [owner]
