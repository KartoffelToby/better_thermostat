"""The room humidity a thermostat publishes follows its sensor's availability.

A sensor that stops reporting leaves the humidity unknown, whether it was
already silent when the thermostat started or fell silent afterwards; the
last reading is not a measurement of the room any more.
"""

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
import pytest

from .conftest import (
    BT_ENTITY,
    HUMIDITY_ID,
    make_entry,
    set_room_humidity,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

SILENT_STATES = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _published_humidity(hass) -> float | None:
    return hass.states.get(BT_ENTITY).attributes.get("current_humidity")


async def _start(hass):
    entry = make_entry(GENERIC_HEAT_TRV, with_humidity=True)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)


@pytest.mark.parametrize("silent", SILENT_STATES)
async def test_a_sensor_silent_at_startup_publishes_no_humidity(hass, fake_trv, silent):
    """A sensor that is silent when the thermostat starts leaves it unknown."""
    set_room_sensor(hass, 19.0)
    hass.states.async_set(HUMIDITY_ID, silent)
    await _start(hass)

    assert _published_humidity(hass) is None


@pytest.mark.parametrize("silent", SILENT_STATES)
async def test_a_sensor_falling_silent_clears_the_published_humidity(
    hass, fake_trv, silent
):
    """A sensor falling silent at runtime clears the humidity; a reading restores it."""
    set_room_sensor(hass, 19.0)
    set_room_humidity(hass, 42.5)
    await _start(hass)
    assert _published_humidity(hass) == 42.5

    hass.states.async_set(HUMIDITY_ID, silent)
    assert await wait_for(hass, lambda: _published_humidity(hass) is None)

    set_room_humidity(hass, 47.0)
    assert await wait_for(hass, lambda: _published_humidity(hass) == 47.0)
