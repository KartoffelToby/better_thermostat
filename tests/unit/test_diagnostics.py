"""Tests for the config-entry diagnostics download."""

import copy

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_thermostat.utils.const import (
    CONF_HEATER,
    CONF_SENSOR,
    DOMAIN,
)


@pytest.mark.asyncio
async def test_a_download_leaves_the_stored_entry_as_it_was(hass):
    """Downloading the diagnostics writes nothing into the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HEATER: [
                {
                    "trv": "climate.trv",
                    "integration": "mqtt",
                    "advanced": {"calibration": 0},
                    "model": "TRVZB",
                }
            ],
            CONF_SENSOR: "sensor.room",
        },
    )
    hass.states.async_set("climate.trv", "heat")
    before = copy.deepcopy(dict(entry.data))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert dict(entry.data) == before
    assert diagnostics["thermostat"]["climate.trv"]["bt_adapter"] == "mqtt"


@pytest.mark.asyncio
async def test_an_entry_without_a_room_sensor_still_downloads(hass):
    """An entry whose room sensor was cleared yields a download, not an error."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HEATER: [
                {
                    "trv": "climate.trv",
                    "integration": "mqtt",
                    "advanced": {},
                    "model": "TRVZB",
                }
            ],
            CONF_SENSOR: None,
        },
    )
    hass.states.async_set("climate.trv", "heat")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["external_temperature_sensor"] is None
    assert "climate.trv" in diagnostics["thermostat"]


@pytest.mark.asyncio
async def test_a_device_bundle_without_integration_or_model_still_downloads(hass):
    """A bundle that lacks the detected integration and model reports them unknown."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HEATER: [{"trv": "climate.trv", "advanced": {}}],
            CONF_SENSOR: "sensor.room",
        },
    )
    hass.states.async_set("climate.trv", "heat")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    trv = diagnostics["thermostat"]["climate.trv"]
    assert trv["bt_adapter"] == "unknown"
    assert trv["bt_integration"] is None
    assert trv["model"] is None
