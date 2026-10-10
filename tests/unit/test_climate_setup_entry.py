"""The climate platform builds its entity from the settings setup parsed.

Setup parses the entry's stored settings once and keeps the result on the
entry's runtime data; the climate entity reads that parsed view rather than
the raw entry. A runtime container built without settings falls back to
parsing the entry itself.
"""

import logging
from unittest.mock import MagicMock, patch

from homeassistant.const import UnitOfTemperature
import pytest

from custom_components.better_thermostat import BetterThermostatData
from custom_components.better_thermostat.climate import (
    _configured_off_temperature,
    _configured_temperature_step,
    _configured_tolerance,
    async_setup_entry,
)
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
    entry.data = dict[str, object]()
    entry.options = RAW
    entry.runtime_data = runtime_data
    return entry


def _hass(unit=UnitOfTemperature.CELSIUS):
    hass = MagicMock()
    hass.config.units.temperature_unit = unit
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


@pytest.mark.asyncio
async def test_the_entity_receives_the_stored_numbers_read_once():
    """Bounds, step, off temperature and tolerance arrive as Celsius numbers."""
    raw = RAW | {
        "target_temp_min": "16.0",
        "target_temp_max": "-1.0",
        "target_temp_step": "0.5",
        "off_temperature": "68",
        "tolerance": "0.9",
        "presets": ["eco"],
    }
    entry = _entry(BetterThermostatData(settings=parse_settings(raw)))

    with patch(_NORMALIZE):
        await async_setup_entry(_hass(UnitOfTemperature.FAHRENHEIT), entry, MagicMock())

    bt = entry.runtime_data.climate
    assert bt.configured_min_temperature == 16.0
    assert bt.configured_max_temperature is None
    assert bt.bt_target_temperature_step == 0.5
    assert bt._configured_temperature_step == 0.5
    assert bt.off_temperature == pytest.approx(20.0)
    assert bt.tolerance == pytest.approx(0.5)
    assert bt._unit is UnitOfTemperature.FAHRENHEIT


@pytest.mark.parametrize("stored", ["0", "0.00", "0.0", ""])
@pytest.mark.asyncio
async def test_a_stored_zero_step_leaves_the_step_automatic(stored):
    """A step stored as any spelling of zero is derived from the devices."""
    entry = _entry(
        BetterThermostatData(
            settings=parse_settings(RAW | {"target_temp_step": stored})
        )
    )

    with patch(_NORMALIZE):
        await async_setup_entry(_hass(), entry, MagicMock())

    bt = entry.runtime_data.climate
    assert bt.bt_target_temperature_step is None
    assert bt._configured_temperature_step is None


def test_an_unreadable_step_is_logged_and_left_automatic(caplog):
    with caplog.at_level(logging.WARNING):
        assert _configured_temperature_step("fine", "Test BT") is None

    assert "invalid target_temp_step 'fine'" in caplog.text


@pytest.mark.parametrize(
    ("stored", "unit", "celsius"),
    [
        (None, UnitOfTemperature.CELSIUS, None),
        ("", UnitOfTemperature.CELSIUS, None),
        ("None", UnitOfTemperature.CELSIUS, None),
        (0, UnitOfTemperature.CELSIUS, 0.0),
        ("20", UnitOfTemperature.CELSIUS, 20.0),
        ("cold", UnitOfTemperature.CELSIUS, None),
        (200.0, UnitOfTemperature.CELSIUS, None),
        (50.0, UnitOfTemperature.FAHRENHEIT, 10.0),
    ],
)
def test_the_off_temperature_is_read_into_celsius(stored, unit, celsius):
    """No value, an unreadable one and an implausible one mean no off temperature."""
    read = _configured_off_temperature(stored, "Test BT", unit)

    assert read == (None if celsius is None else pytest.approx(celsius))


@pytest.mark.parametrize(
    ("stored", "unit", "celsius"),
    [
        (None, UnitOfTemperature.CELSIUS, 0.0),
        ("0.5", UnitOfTemperature.CELSIUS, 0.5),
        (0.9, UnitOfTemperature.FAHRENHEIT, 0.5),
        ("wide", UnitOfTemperature.CELSIUS, 0.0),
        ("nan", UnitOfTemperature.CELSIUS, 0.0),
        (-1.0, UnitOfTemperature.CELSIUS, 0.0),
        (12.0, UnitOfTemperature.CELSIUS, 12.0),
    ],
)
def test_the_tolerance_is_read_into_a_celsius_delta(stored, unit, celsius):
    """No value, an unreadable one and a negative one mean no tolerance."""
    assert _configured_tolerance(stored, "Test BT", unit) == pytest.approx(celsius)
