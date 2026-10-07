"""The climate platform builds its entity from the settings setup parsed.

Setup parses the entry's stored settings once and keeps the result on the
entry's runtime data; the climate entity reads that parsed view rather than
the raw entry. A runtime container built without settings falls back to
parsing the entry itself.
"""

from unittest.mock import MagicMock, patch

from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat import BetterThermostatData
from custom_components.better_thermostat.climate import async_setup_entry
from custom_components.better_thermostat.utils.entry_schema import parse_settings

_NORMALIZE = "custom_components.better_thermostat.climate.async_normalize_bt_entity_ids"

RAW = {
    "name": "Living room",
    "thermostat": [
        {
            "trv": "climate.trv",
            "integration": "generic_thermostat",
            "model": "Generic",
            "adapter": None,
            "advanced": {"child_lock": "false", "calibration": "target_temp_based"},
        }
    ],
    "window_off_delay": 30,
    "tolerance": 0.3,
}


def _entry(runtime_data):
    entry = MagicMock()
    entry.entry_id = "entry"
    entry.data = {}
    entry.options = RAW
    entry.runtime_data = runtime_data
    return entry


def _hass():
    hass = MagicMock()
    hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    return hass


@pytest.mark.asyncio
async def test_the_entity_is_built_from_the_parsed_settings():
    """The thermostat list the entity holds is the parsed one, not the raw one."""
    settings = parse_settings(RAW)
    entry = _entry(BetterThermostatData(settings=settings))
    add_entities = MagicMock()

    with patch(_NORMALIZE):
        await async_setup_entry(_hass(), entry, add_entities)

    bt = entry.runtime_data.climate
    add_entities.assert_called_once_with([bt])
    assert bt.device_name == "Living room"
    assert bt.all_trvs is settings["thermostat"]
    assert bt.all_trvs[0]["advanced"]["child_lock"] is False
    assert bt.window_open_delay_seconds == 30.0


@pytest.mark.asyncio
async def test_a_container_without_settings_parses_the_entry():
    """Without parsed settings on the runtime data, the entry is parsed here."""
    entry = _entry(BetterThermostatData())

    with patch(_NORMALIZE):
        await async_setup_entry(_hass(), entry, MagicMock())

    bt = entry.runtime_data.climate
    assert bt.all_trvs == parse_settings(RAW)["thermostat"]
    assert entry.options is RAW
    assert RAW["thermostat"][0]["advanced"]["child_lock"] == "false"
