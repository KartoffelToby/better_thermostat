"""A stored outdoor threshold below freezing is announced at setup.

The outdoor threshold is stored in the system unit. On a Fahrenheit system an
entry can carry a number that was meant as Celsius, such as 20, which reads
as 20 °F (-6.7 °C) and switches the heating off whenever it is warmer than
that outside. The stored value is the user's to change, so setup names the
entry and the value in one warning and leaves the value as it is.
"""

import logging

from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

ENTRY_NAME = "BT Test"
OUTDOOR_ID = "sensor.outdoor_temperature"
WEATHER_ID = "weather.home"


def _refused_writes(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "is not valid" in record.getMessage()
    ]


def _threshold_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("custom_components.better_thermostat")
        and "off_temperature" in record.getMessage()
    ]


async def _start(hass, trv, unit_system, off_temperature, source):
    """Set up an entry with ``off_temperature`` and the outdoor ``source``."""
    hass.config.units = unit_system
    # The device published its state under the previous unit system; it
    # republishes so its temperatures are read in the one set here.
    trv.async_write_ha_state()
    unit = unit_system.temperature_unit
    hass.states.async_set(
        SENSOR_ID, "66.0" if unit == "°F" else "19.0", {"unit_of_measurement": unit}
    )
    hass.states.async_set(OUTDOOR_ID, "50", {"unit_of_measurement": unit})
    hass.states.async_set(WEATHER_ID, "cloudy", {"temperature": 50})
    base = make_entry()
    data = {**base.data, "off_temperature": off_temperature}
    if source == "outdoor_sensor":
        data["outdoor_sensor"] = OUTDOOR_ID
    elif source == "weather":
        data["weather"] = WEATHER_ID
    entry = MockConfigEntry(
        domain=DOMAIN, version=base.version, data=data, title=base.title
    )
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


@pytest.mark.parametrize("source", ["outdoor_sensor", "weather"])
async def test_a_threshold_below_freezing_is_named_once(hass, fake_trv, caplog, source):
    """20 °F on a Fahrenheit system yields one warning naming entry and value."""
    caplog.set_level(logging.INFO)
    bt = await _start(hass, fake_trv, US_CUSTOMARY_SYSTEM, 20, source)

    # Further outdoor readings run the threshold check again.
    for reading in ("51", "52"):
        hass.states.async_set(OUTDOOR_ID, reading, {"unit_of_measurement": "°F"})
        await hass.async_block_till_done()
    assert await wait_for(hass, lambda: not bt.startup_running)

    warnings = _threshold_warnings(caplog)
    assert len(warnings) == 1, warnings
    assert ENTRY_NAME in warnings[0]
    assert "20" in warnings[0]
    assert "-6.7" in warnings[0]
    # The stored value is left for the user to change.
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.data["off_temperature"] == 20
    assert _refused_writes(caplog) == []


@pytest.mark.parametrize(
    ("unit_system", "off_temperature", "source"),
    [
        pytest.param(US_CUSTOMARY_SYSTEM, 50, "outdoor_sensor", id="fahrenheit-10C"),
        pytest.param(US_CUSTOMARY_SYSTEM, 32, "outdoor_sensor", id="fahrenheit-0C"),
        pytest.param(US_CUSTOMARY_SYSTEM, 20, None, id="fahrenheit-no-source"),
        pytest.param(METRIC_SYSTEM, 20, "outdoor_sensor", id="celsius-20"),
    ],
)
async def test_a_threshold_that_acts_as_meant_stays_quiet(
    hass, fake_trv, caplog, unit_system, off_temperature, source
):
    """Thresholds at or above freezing, or without an outdoor source, warn nothing."""
    caplog.set_level(logging.INFO)
    await _start(hass, fake_trv, unit_system, off_temperature, source)

    assert _threshold_warnings(caplog) == []
    assert _refused_writes(caplog) == []
