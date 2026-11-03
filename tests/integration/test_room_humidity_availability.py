"""The room humidity a thermostat publishes follows its sensor's availability.

A sensor that stops reporting leaves the humidity unknown, whether it was
already silent when the thermostat started or fell silent afterwards; the
last reading is not a measurement of the room any more.
"""

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
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

BT_ENTITY = "climate.bt_test"
HUMIDITY_ID = "sensor.room_humidity"
SILENT_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _set_humidity(hass, value) -> None:
    hass.states.async_set(HUMIDITY_ID, str(value), {"unit_of_measurement": "%"})


def _published_humidity(hass) -> float | None:
    return hass.states.get(BT_ENTITY).attributes.get("current_humidity")


async def _start(hass):
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    base = make_entry()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=base.version,
        data={**base.data, "humidity_sensor": HUMIDITY_ID},
        title=base.title,
    )
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)


@pytest.mark.parametrize("silent", SILENT_STATES)
async def test_a_sensor_silent_at_startup_publishes_no_humidity(hass, fake_trv, silent):
    """A sensor that is silent when the thermostat starts leaves it unknown."""
    hass.states.async_set(HUMIDITY_ID, silent)
    await _start(hass)

    assert _published_humidity(hass) is None


@pytest.mark.parametrize("silent", SILENT_STATES)
async def test_a_sensor_falling_silent_clears_the_published_humidity(
    hass, fake_trv, silent
):
    """A sensor falling silent at runtime clears the humidity; a reading restores it."""
    _set_humidity(hass, 42.5)
    await _start(hass)
    assert _published_humidity(hass) == 42.5

    hass.states.async_set(HUMIDITY_ID, silent)
    assert await wait_for(hass, lambda: _published_humidity(hass) is None)

    _set_humidity(hass, 47.0)
    assert await wait_for(hass, lambda: _published_humidity(hass) == 47.0)


async def test_a_removed_sensor_clears_the_published_humidity(hass, fake_trv):
    """A sensor removed at runtime clears the humidity; a reading restores it."""
    _set_humidity(hass, 42.5)
    await _start(hass)
    assert _published_humidity(hass) == 42.5

    hass.states.async_remove(HUMIDITY_ID)
    assert await wait_for(hass, lambda: _published_humidity(hass) is None)

    _set_humidity(hass, 47.0)
    assert await wait_for(hass, lambda: _published_humidity(hass) == 47.0)


async def test_the_humidity_follows_its_sensor_while_a_head_is_unavailable(
    hass, fake_trv
):
    """With the head off the air, the humidity still clears and updates."""
    _set_humidity(hass, 42.5)
    await _start(hass)
    assert _published_humidity(hass) == 42.5

    fake_trv._attr_available = False
    fake_trv.async_write_ha_state()
    await hass.async_block_till_done()
    hass.states.async_remove(HUMIDITY_ID)
    assert await wait_for(hass, lambda: _published_humidity(hass) is None)

    _set_humidity(hass, 47.0)
    assert await wait_for(hass, lambda: _published_humidity(hass) == 47.0)
