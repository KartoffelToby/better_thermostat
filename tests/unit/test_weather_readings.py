"""What the calibration reads off the weather: sunshine and the outdoor temperature.

Weather integrations publish different subsets of the same information,
some only in their forecast. The sunshine estimate takes the first source
that says something, cloud cover before UV index before the condition; the
outdoor temperature prefers a dedicated sensor over the weather entity.
"""

from homeassistant.const import UnitOfTemperature
from homeassistant.core import State
import pytest

from custom_components.better_thermostat.calibration import (
    _get_current_outdoor_temp,
    _get_current_solar_intensity,
)
from tests.factories import ThermostatStandIn

WEATHER = "weather.home"
OUTDOOR = "sensor.outdoor"


def _bt(*states: State, weather: str | None = WEATHER, outdoor: str | None = None):
    """Return a thermostat that reads ``states`` from Home Assistant."""
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.weather_entity_id = weather
    bt.outdoor_sensor_entity_id = outdoor
    published = {state.entity_id: state for state in states}
    bt.hass.states.get.side_effect = published.get
    return bt


@pytest.mark.parametrize(
    ("condition", "attributes", "intensity"),
    [
        pytest.param("rainy", {"cloud_coverage": 25}, 0.75, id="cloud-cover"),
        pytest.param(
            "rainy",
            {"forecast": [{"cloud_coverage": 60}]},
            0.4,
            id="cloud-cover-from-the-forecast",
        ),
        pytest.param(
            "rainy",
            {"cloud_coverage": "n/a", "uv_index": 6},
            0.6,
            id="unreadable-cloud-cover-falls-to-uv",
        ),
        pytest.param(
            "rainy",
            {"uv_index": "n/a", "friendly_name": "Home"},
            0.1,
            id="unreadable-uv-falls-to-the-condition",
        ),
        pytest.param("sunny", {"friendly_name": "Home"}, 1.0, id="sunny"),
        pytest.param("partlycloudy", {"friendly_name": "Home"}, 0.7, id="partly"),
        pytest.param("cloudy", {"friendly_name": "Home"}, 0.4, id="cloudy"),
        pytest.param("rainy", {"friendly_name": "Home"}, 0.1, id="rain"),
        pytest.param(
            "unknown",
            {"forecast": [{"condition": "cloudy"}]},
            0.4,
            id="condition-from-the-forecast",
        ),
        pytest.param(
            "cloudy",
            {"forecast": ["not a forecast entry"]},
            0.4,
            id="malformed-forecast-entry",
        ),
    ],
)
def test_the_sunshine_comes_from_the_first_source_that_says_something(
    condition, attributes, intensity
):
    bt = _bt(State(WEATHER, condition, attributes))

    assert _get_current_solar_intensity(bt) == pytest.approx(intensity)


@pytest.mark.parametrize(
    "bt",
    [
        pytest.param(_bt(weather=None), id="no-weather-entity"),
        pytest.param(_bt(), id="weather-entity-not-there"),
    ],
)
def test_without_a_weather_reading_there_is_no_sunshine(bt):
    assert _get_current_solar_intensity(bt) == 0.0


def test_the_outdoor_sensor_wins_over_the_weather():
    bt = _bt(
        State(OUTDOOR, "7.5", {"unit_of_measurement": UnitOfTemperature.CELSIUS}),
        State(WEATHER, "sunny", {"temperature": 2.0, "temperature_unit": "°C"}),
        outdoor=OUTDOOR,
    )

    assert _get_current_outdoor_temp(bt) == 7.5


def test_an_outdoor_sensor_that_is_not_there_leaves_the_weather_to_say():
    """The weather's temperature is read in its own unit."""
    bt = _bt(
        State(
            WEATHER,
            "sunny",
            {"temperature": 50.0, "temperature_unit": UnitOfTemperature.FAHRENHEIT},
        ),
        outdoor=OUTDOOR,
    )

    assert _get_current_outdoor_temp(bt) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "bt",
    [
        pytest.param(_bt(weather=None), id="nothing-configured"),
        pytest.param(_bt(), id="weather-entity-not-there"),
    ],
)
def test_without_an_outdoor_reading_there_is_no_outdoor_temperature(bt):
    assert _get_current_outdoor_temp(bt) is None
