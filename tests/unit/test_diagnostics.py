"""Tests for the config-entry diagnostics."""

import copy
from unittest.mock import MagicMock

from homeassistant.core import State
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import DOMAIN
from custom_components.better_thermostat.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR


def _hass():
    hass = MagicMock()
    hass.states.get.return_value = State(
        "climate.trv", "heat", {"friendly_name": "TRV", "temperature": 21.0}
    )
    return hass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("integration", "expected_adapter"), [("mqtt", "mqtt"), (None, "unknown")]
)
async def test_diagnostics_leave_the_entry_data_untouched(
    integration, expected_adapter
):
    """A diagnostics download leaves the stored entry configuration as it was.

    The TRV dicts inside ``entry.data`` are the stored configuration; a key
    added to them here would be persisted with the next entry update.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="entry-1",
        data={
            CONF_HEATER: [
                {
                    "trv": "climate.trv",
                    "integration": integration,
                    "advanced": {"calibration": 0},
                    "model": "TRVZB",
                }
            ],
            CONF_SENSOR: "sensor.room",
        },
    )
    before = copy.deepcopy(dict(entry.data))

    diagnostics = await async_get_config_entry_diagnostics(_hass(), entry)

    assert dict(entry.data) == before
    assert "adapter" not in entry.data[CONF_HEATER][0]
    assert diagnostics["thermostat"]["climate.trv"]["bt_adapter"] == expected_adapter


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
