"""The solar intensity sensor follows the weather entity it is computed from."""

import copy
from datetime import timedelta

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from .conftest import DOMAIN, SENSOR_ID, make_entry, setup_entry, wait_for_startup

WEATHER = "weather.home"


async def _sensor_with_weather(hass, cloud_coverage):
    """Set a thermostat up on a weather entity and return its solar sensor id."""
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    hass.states.async_set(WEATHER, "cloudy", {"cloud_coverage": cloud_coverage})
    entry = make_entry()
    data = copy.deepcopy(dict(entry.data))
    data["weather"] = WEATHER
    entry = MockConfigEntry(
        domain=DOMAIN, version=entry.version, data=data, title=entry.title
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{bt.unique_id}_solar_intensity"
    )
    assert entity_id is not None
    return entity_id


async def test_the_sensor_follows_a_weather_change(hass, fake_trv):
    """A new cloud coverage shows in the sensor without a thermostat change."""
    sensor = await _sensor_with_weather(hass, 20)
    assert float(hass.states.get(sensor).state) == 80.0

    hass.states.async_set(WEATHER, "cloudy", {"cloud_coverage": 70})
    await hass.async_block_till_done()

    assert float(hass.states.get(sensor).state) == 30.0


async def test_the_sensor_is_not_rewritten_while_nothing_changes(hass, fake_trv):
    """Time passing without a weather or thermostat change writes no state."""
    sensor = await _sensor_with_weather(hass, 20)
    reported = hass.states.get(sensor).last_reported

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=65))
    await hass.async_block_till_done()

    assert hass.states.get(sensor).last_reported == reported
