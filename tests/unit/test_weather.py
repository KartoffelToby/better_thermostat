"""Tests for utils/weather.py — call-for-heat decisions from weather data.

The module has three public coroutines:

* ``check_weather`` — orchestrates the call-for-heat decision from a weather
  entity and/or an outdoor sensor.
* ``check_weather_prediction`` — evaluates a weather entity's forecast.
* ``check_ambient_air_temperature`` — damps an outdoor sensor's readings,
  filled from its recorder history when available.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported
import pytest
from sqlalchemy.exc import OperationalError

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.outdoor import add_reading, start_damping
from custom_components.better_thermostat.utils.weather import (
    FORECAST_CALL_TIMEOUT,
    OUTDOOR_HISTORY_RETRY,
    OUTDOOR_HISTORY_WINDOW,
    check_ambient_air_temperature,
    check_weather,
    check_weather_prediction,
    summer_mode_facts,
)

WEATHER_ID = "weather.home"
OUTDOOR_ID = "sensor.outdoor"

WEATHER_MOD = "custom_components.better_thermostat.utils.weather"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# FakeClock's epoch: a state stamped here is current when a check runs.
NOW = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_state(state="20.0", attrs=None, last_updated=NOW):
    """Build a minimal stand-in for a HA State with state and attributes."""
    s = MagicMock()
    s.state = state
    s.attributes = attrs if attrs is not None else {}
    s.last_updated = last_updated
    return s


def make_hass(states=None, forecast_response=None, components=None):
    """Build a hass mock wiring states.get and services.async_call."""
    states = states or {}
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=states.get)
    hass.services.async_call = AsyncMock(return_value=forecast_response)
    hass.config.components = components if components is not None else set()
    return hass


def make_bt(hass, **kw):
    """Build a BetterThermostat stand-in carrying the attrs weather.py touches."""
    bt = SimpleNamespace(
        hass=hass,
        entity_id="climate.test_bt",
        device_name="Test BT",
        weather_entity_id=None,
        outdoor_sensor_entity_id=None,
        off_temperature=10.0,
        damped_outdoor_temperature=None,
        call_for_heat=True,
        clock=FakeClock(),
        weather_verdict_missing_since=None,
        weather_fallback_active=False,
        outdoor_damping=None,
        outdoor_history_damped=False,
        outdoor_history_read_at=None,
        outdoor_history_failing=False,
        _outdoor_check_lock=None,
    )
    for k, v in kw.items():
        setattr(bt, k, v)
    return bt


async def hanging_service_call(*_args, **_kwargs):
    """Stand in for a service call whose handler never returns."""
    await asyncio.Event().wait()


def weather_state(
    features=int(WeatherEntityFeature.FORECAST_DAILY), temperature=20.0, unit="°C"
):
    """Build a weather entity state advertising forecast support and a temperature."""
    return make_state(
        state="cloudy",
        attrs={
            "supported_features": features,
            "temperature": temperature,
            "temperature_unit": unit,
        },
    )


def forecast_resp(entity_id, temps, unit=None):
    """Wrap a list of temps as a get_forecasts service response."""
    items = []
    for t in temps:
        entry = {"temperature": t}
        if unit is not None:
            entry["temperature_unit"] = unit
        items.append(entry)
    return {entity_id: {"forecast": items}}


# ===========================================================================
# check_weather_prediction
# ===========================================================================


class TestCheckWeatherPrediction:
    """Forecast evaluation: a missing entity, missing config, and forecasts."""

    async def test_no_weather_entity_returns_false(self):
        """Without a weather entity the prediction is False."""
        bt = make_bt(make_hass(), weather_entity_id=None)
        assert await check_weather_prediction(bt) is False

    async def test_missing_off_temperature_gives_no_opinion(self):
        """Without an off_temperature the forecast has nothing to be compared to.

        A missing threshold is a configuration gap, not a weather verdict, so
        the prediction answers None like every other case it cannot decide.
        """
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID, off_temperature=None)
        assert await check_weather_prediction(bt) is None

    async def test_no_forecast_support_returns_none(self):
        """An entity without any forecast feature yields None (no opinion)."""
        states = {WEATHER_ID: weather_state(features=0)}
        bt = make_bt(make_hass(states=states), weather_entity_id=WEATHER_ID)
        assert await check_weather_prediction(bt) is None

    async def test_cold_forecast_calls_for_heat(self):
        """A forecast below the threshold calls for heat."""
        states = {WEATHER_ID: weather_state(temperature=2.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [1.0, 1.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_warm_forecast_and_warm_now_no_heat(self):
        """A warm forecast and warm current temperature do not call for heat."""
        states = {WEATHER_ID: weather_state(temperature=18.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [17.0, 16.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is False

    async def test_current_temp_below_off_drives_heat_even_if_forecast_warm(self):
        """A cold current temperature alone is enough to call for heat."""
        states = {WEATHER_ID: weather_state(temperature=2.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [20.0, 20.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    @pytest.mark.parametrize(
        ("call_for_heat", "expected"),
        [
            pytest.param(True, True, id="heating_keeps_heating"),
            pytest.param(False, False, id="summer_mode_stays_on"),
        ],
    )
    async def test_a_forecast_inside_the_hysteresis_band_keeps_the_mode(
        self, call_for_heat, expected
    ):
        """Half a kelvin below the threshold neither stops nor resumes heating."""
        states = {WEATHER_ID: weather_state(temperature=9.5)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [9.5, 9.5])
        )
        bt = make_bt(
            hass,
            weather_entity_id=WEATHER_ID,
            off_temperature=10.0,
            call_for_heat=call_for_heat,
        )
        assert await check_weather_prediction(bt) is expected

    async def test_a_forecast_below_the_band_resumes_heating(self):
        """A room in summer mode heats once the forecast drops below the band."""
        states = {WEATHER_ID: weather_state(temperature=8.9)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [8.9, 8.9])
        )
        bt = make_bt(
            hass,
            weather_entity_id=WEATHER_ID,
            off_temperature=10.0,
            call_for_heat=False,
        )
        assert await check_weather_prediction(bt) is True

    async def test_fahrenheit_forecast_is_converted(self):
        """Fahrenheit forecast temps are converted to Celsius before comparing."""
        # 32 °F == 0 °C, well below a 10 °C threshold.
        states = {WEATHER_ID: weather_state(temperature=50.0, unit="°F")}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(
                WEATHER_ID, [32.0, 32.0], unit=UnitOfTemperature.FAHRENHEIT
            )
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_forecast_entry_without_string_unit_uses_entity_unit(self):
        """A forecast entry whose unit is not a string reads in the entity's unit."""
        # 32 °F == 0 °C, below the threshold; 50 °F == 10 °C is not.
        states = {WEATHER_ID: weather_state(temperature=50.0, unit="°F")}
        hass = make_hass(states=states)
        response = forecast_resp(WEATHER_ID, [32.0, 32.0])
        for entry in response[WEATHER_ID]["forecast"]:
            entry["temperature_unit"] = None
        hass.services.async_call = AsyncMock(return_value=response)
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_empty_forecast_returns_none(self):
        """An empty forecast list resolves to None (no opinion)."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value={WEATHER_ID: {"forecast": []}}
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID)
        assert await check_weather_prediction(bt) is None

    async def test_service_error_returns_none(self):
        """A HomeAssistantError from the service resolves to None."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("boom"))
        bt = make_bt(hass, weather_entity_id=WEATHER_ID)
        assert await check_weather_prediction(bt) is None

    async def test_a_hanging_service_gives_no_opinion_after_the_timeout(self, caplog):
        """A forecast service that stalls yields no verdict once the timeout ends.

        A cloud weather integration can stall on its request while the
        internet is down. The call is cut off after the forecast timeout and
        reads as a missing forecast, with a warning naming the entity.
        """
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(side_effect=hanging_service_call)
        bt = make_bt(hass, weather_entity_id=WEATHER_ID)
        with (
            patch(f"{WEATHER_MOD}.FORECAST_CALL_TIMEOUT", timedelta(seconds=0.01)),
            caplog.at_level(logging.WARNING, logger=WEATHER_MOD),
        ):
            result = await asyncio.wait_for(check_weather_prediction(bt), timeout=5)
        assert result is None
        assert _weather_records(caplog, logging.WARNING)

    async def test_the_forecast_call_is_bounded_by_default(self):
        """The shipped timeout holds startup up for at most ten seconds."""
        assert FORECAST_CALL_TIMEOUT == timedelta(seconds=10)

    async def test_service_not_supported_returns_none(self):
        """A ServiceNotSupported error resolves to None."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            side_effect=ServiceNotSupported("weather", "get_forecasts", WEATHER_ID)
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID)
        assert await check_weather_prediction(bt) is None

    async def test_forecast_temps_are_averaged(self):
        """Up to two forecast temps are averaged before the comparison.

        Forecast = [15, 1] with off_temperature 10 and a warm current temperature:
        the mean (8) is below the threshold, so heating is requested.
        """
        states = {WEATHER_ID: weather_state(temperature=15.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [15.0, 1.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_daily_forecast_samples_two_entries(self):
        """A daily forecast averages the first two entries (~two days)."""
        states = {WEATHER_ID: weather_state(temperature=15.0)}
        hass = make_hass(states=states)
        # First two warm, later days freezing -> beyond the two-day horizon.
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [15.0, 15.0, -30.0, -30.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is False

    async def test_hourly_forecast_samples_beyond_two_entries(self):
        """An hourly forecast samples well past the first two hours.

        With the first two hours warm and the rest freezing, the wider horizon
        averages below the threshold, so heating is requested.
        """
        states = {
            WEATHER_ID: weather_state(
                features=int(WeatherEntityFeature.FORECAST_HOURLY), temperature=15.0
            )
        }
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [15.0, 15.0, -30.0, -30.0, -30.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_forecast_entry_missing_temperature_is_filtered(self):
        """A forecast entry without a temperature is filtered out."""
        states = {WEATHER_ID: weather_state(temperature=15.0)}
        hass = make_hass(states=states)
        # First entry has no temperature -> filtered; second is freezing.
        hass.services.async_call = AsyncMock(
            return_value={WEATHER_ID: {"forecast": [{"foo": 1}, {"temperature": 1.0}]}}
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_a_forecast_without_any_usable_temperature_gives_no_opinion(self):
        """No usable reading anywhere is no verdict, not a warm forecast.

        The entity returns forecast entries, but none carries a temperature,
        and the entity reports no current temperature either. Answering False
        would switch a weather-only room into summer mode on no data.
        """
        states = {WEATHER_ID: weather_state(temperature=None)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value={
                WEATHER_ID: {"forecast": [{"foo": 1}, {"condition": "sunny"}]}
            }
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is None

    async def test_a_warm_current_reading_decides_when_the_forecast_has_none(self):
        """A usable current temperature still gives a verdict on its own."""
        states = {WEATHER_ID: weather_state(temperature=18.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value={
                WEATHER_ID: {"forecast": [{"foo": 1}, {"condition": "sunny"}]}
            }
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is False

    async def test_twice_daily_forecast_is_used(self):
        """An entity advertising twice-daily forecasts uses that type."""
        states = {
            WEATHER_ID: weather_state(
                features=int(WeatherEntityFeature.FORECAST_TWICE_DAILY), temperature=2.0
            )
        }
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [1.0, 1.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True
        assert hass.services.async_call.call_args[0][2]["type"] == "twice_daily"

    async def test_feature_selection_prefers_daily(self):
        """When several forecast features are advertised, daily wins."""
        feats = int(
            WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
        )
        states = {WEATHER_ID: weather_state(features=feats, temperature=2.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [1.0, 1.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=10.0)
        await check_weather_prediction(bt)
        # The service was asked for the daily forecast type.
        called_with = hass.services.async_call.call_args[0]
        assert called_with[2]["type"] == "daily"


# ===========================================================================
# check_ambient_air_temperature
# ===========================================================================


class TestCheckAmbientAirTemperature:
    """Outdoor sensor evaluation, with and without recorder history."""

    async def test_no_outdoor_sensor_returns_none(self):
        """Without an outdoor sensor the check is a no-op."""
        bt = make_bt(make_hass(), outdoor_sensor_entity_id=None)
        assert await check_ambient_air_temperature(bt) is None

    async def test_off_temperature_not_float_returns_none(self):
        """A missing off_temperature short-circuits to None."""
        bt = make_bt(
            make_hass(), outdoor_sensor_entity_id=OUTDOOR_ID, off_temperature=None
        )
        assert await check_ambient_air_temperature(bt) is None

    async def test_unavailable_sensor_without_cache_forces_heat(self):
        """An unavailable sensor with no cached value forces heating on."""
        states = {OUTDOOR_ID: make_state(state="unavailable")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=None,
            call_for_heat=False,
        )
        assert await check_ambient_air_temperature(bt) is None
        assert bt.call_for_heat is True

    async def test_unavailable_sensor_with_cache_keeps_state(self):
        """An unavailable sensor with a cached value leaves call_for_heat alone."""
        states = {OUTDOOR_ID: make_state(state="unknown")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=8.0,
            call_for_heat=False,
        )
        assert await check_ambient_air_temperature(bt) is None
        # Existing cache -> call_for_heat is left untouched.
        assert bt.call_for_heat is False

    async def test_missing_sensor_state_treated_as_unavailable(self):
        """A missing sensor state is handled like an unavailable one."""
        # states.get returns None for the sensor.
        bt = make_bt(
            make_hass(states={}),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=None,
            call_for_heat=False,
        )
        assert await check_ambient_air_temperature(bt) is None
        assert bt.call_for_heat is True

    async def test_no_recorder_uses_current_reading_cold(self):
        """Without recorder, a cold current reading calls for heat."""
        states = {
            OUTDOOR_ID: make_state(state="5.0", attrs={"unit_of_measurement": "°C"})
        }
        bt = make_bt(
            make_hass(states=states, components=set()),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=10.0,
        )
        await check_ambient_air_temperature(bt)
        assert bt.call_for_heat is True
        assert bt.damped_outdoor_temperature == 5.0

    async def test_no_recorder_uses_current_reading_warm(self):
        """Without recorder, a warm current reading stops heating."""
        states = {
            OUTDOOR_ID: make_state(state="18.0", attrs={"unit_of_measurement": "°C"})
        }
        bt = make_bt(
            make_hass(states=states, components=set()),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=10.0,
        )
        await check_ambient_air_temperature(bt)
        assert bt.call_for_heat is False
        assert bt.damped_outdoor_temperature == 18.0

    async def test_no_recorder_fahrenheit_current_reading(self):
        """A Fahrenheit current reading is converted before comparison."""
        states = {
            OUTDOOR_ID: make_state(
                state="50.0",
                attrs={"unit_of_measurement": UnitOfTemperature.FAHRENHEIT},
            )
        }
        bt = make_bt(
            make_hass(states=states, components=set()),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=5.0,
        )
        await check_ambient_air_temperature(bt)
        # 50 °F == 10 °C, above a 5 °C threshold -> no heat.
        assert bt.damped_outdoor_temperature == pytest.approx(10.0)
        assert bt.call_for_heat is False

    async def test_a_sensor_without_a_numeric_reading_keeps_the_room_heating(self):
        """With no usable reading anywhere there is nothing to damp: the room heats."""
        bt = make_bt(
            make_hass(states={OUTDOOR_ID: make_state(state="error")}, components=set()),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=10.0,
            call_for_heat=False,
            damped_outdoor_temperature=12.0,
        )
        await check_ambient_air_temperature(bt)
        assert bt.damped_outdoor_temperature is None
        assert bt.call_for_heat is True

    async def test_one_warm_reading_does_not_switch_the_room_off(self):
        """Without a recorder the live readings still pass through the filter.

        A reading counts from the moment it arrives, so a jump from 2 °C to
        18 °C leaves the damped temperature at 2 °C until time has passed.
        """
        bt = make_bt(
            make_hass(
                states={
                    OUTDOOR_ID: make_state(
                        state="2.0", attrs={"unit_of_measurement": "°C"}
                    )
                },
                components=set(),
            ),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=10.0,
        )
        await check_ambient_air_temperature(bt)
        bt.clock.advance(HOUR_S)
        bt.hass.states.get = MagicMock(
            return_value=make_state(
                state="18.0",
                attrs={"unit_of_measurement": "°C"},
                last_updated=NOW + timedelta(hours=1),
            )
        )
        await check_ambient_air_temperature(bt)
        assert bt.damped_outdoor_temperature == pytest.approx(2.0)
        assert bt.call_for_heat is True

    @pytest.mark.parametrize(
        ("call_for_heat", "reading", "expected"),
        [
            pytest.param(True, "9.5", True, id="heating_inside_the_band"),
            pytest.param(False, "9.5", False, id="summer_mode_inside_the_band"),
            pytest.param(False, "8.9", True, id="summer_mode_below_the_band"),
        ],
    )
    async def test_the_ambient_check_has_a_hysteresis_band(
        self, call_for_heat, reading, expected
    ):
        """The sensor check applies the same band as check_weather."""
        bt = make_bt(
            make_hass(
                states={
                    OUTDOOR_ID: make_state(
                        state=reading, attrs={"unit_of_measurement": "°C"}
                    )
                }
            ),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=10.0,
            call_for_heat=call_for_heat,
        )
        await check_ambient_air_temperature(bt)
        assert bt.call_for_heat is expected

    def _hist_item(self, state, ts, unit="°C"):
        """Build a recorder history item stand-in."""
        it = MagicMock()
        it.state = state
        it.last_updated = ts
        it.attributes = {"unit_of_measurement": unit}
        return it

    def _recorder_bt(self, reading="5.0", off_temperature=10.0, last_updated=NOW):
        """Build a BT whose outdoor sensor reads ``reading`` with a recorder."""
        states = {
            OUTDOOR_ID: make_state(
                state=reading,
                attrs={"unit_of_measurement": "°C"},
                last_updated=last_updated,
            )
        }
        hass = make_hass(states=states, components={"recorder"})
        return make_bt(
            hass, outdoor_sensor_entity_id=OUTDOOR_ID, off_temperature=off_temperature
        )

    async def _check_with_history(self, bt, items):
        """Run one check against a recorder that returns ``items``."""
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            query = AsyncMock(return_value={OUTDOOR_ID: items})
            gi.return_value.async_add_executor_job = query
            await check_ambient_air_temperature(bt)
        return query

    async def test_recorder_history_fills_the_filter(self):
        """Each recorded reading counts for as long as it was current.

        2 °C held for two days, then 20 °C for one time constant: the damped
        temperature has moved 63 % of the way from 2 °C to 20 °C.
        """
        bt = self._recorder_bt(reading="20.0", last_updated=NOW - timedelta(hours=24))
        items = [
            self._hist_item("2.0", NOW - timedelta(hours=72)),
            self._hist_item("20.0", NOW - timedelta(hours=24)),
        ]
        query = await self._check_with_history(bt, items)
        _hass, start, end, entity_id = query.await_args.args[1:]
        assert (end - start, entity_id) == (timedelta(hours=72), OUTDOOR_ID)
        assert OUTDOOR_HISTORY_WINDOW == timedelta(hours=72)
        assert bt.damped_outdoor_temperature == pytest.approx(
            2.0 + 18.0 * (1 - math.exp(-1))
        )
        assert bt.call_for_heat is False

    async def test_recorder_history_weighs_readings_by_duration_not_count(self):
        """A burst of warm readings weighs as long as it lasted (issue #2645).

        A sun-exposed sensor reports often while the sun heats it. Fifty warm
        readings within one minute of an otherwise cold day must not carry
        the day: counted per reading they would average about 29 °C.
        """
        burst_at = NOW - timedelta(hours=24)
        items = [self._hist_item("8.0", NOW - timedelta(hours=48))]
        items += [
            self._hist_item("30.0", burst_at + timedelta(seconds=i)) for i in range(50)
        ]
        items.append(self._hist_item("8.0", burst_at + timedelta(minutes=1)))
        bt = self._recorder_bt(
            reading="8.0", last_updated=burst_at + timedelta(minutes=1)
        )
        await self._check_with_history(bt, items)
        assert bt.damped_outdoor_temperature == pytest.approx(8.0, abs=0.02)
        assert bt.call_for_heat is True

    async def test_recorder_history_order_does_not_matter(self):
        """Recorded states are damped in time order, whatever order they come in."""
        items = [
            self._hist_item("20.0", NOW - timedelta(hours=24)),
            self._hist_item("2.0", NOW - timedelta(hours=72)),
        ]
        bt = self._recorder_bt(reading="20.0", last_updated=NOW - timedelta(hours=24))
        await self._check_with_history(bt, items)
        assert bt.damped_outdoor_temperature == pytest.approx(
            2.0 + 18.0 * (1 - math.exp(-1))
        )

    async def test_recorder_history_filters_bad_states(self):
        """Unknown/unavailable/non-numeric history states are filtered out."""
        hour_ago = NOW - timedelta(hours=1)
        items = [
            self._hist_item("unavailable", hour_ago - timedelta(minutes=3)),
            self._hist_item("unknown", hour_ago - timedelta(minutes=2)),
            self._hist_item("not-a-number", hour_ago - timedelta(minutes=1)),
            self._hist_item("4.0", hour_ago),  # the only usable reading
        ]
        bt = self._recorder_bt(reading="4.0", last_updated=hour_ago)
        await self._check_with_history(bt, items)
        assert bt.damped_outdoor_temperature == pytest.approx(4.0)
        assert bt.call_for_heat is True

    async def test_recorder_fahrenheit_history_is_converted(self):
        """A recorded Fahrenheit reading is damped in Celsius."""
        day_ago = NOW - timedelta(hours=24)
        items = [self._hist_item("50.0", day_ago, unit=UnitOfTemperature.FAHRENHEIT)]
        bt = self._recorder_bt(reading="10.0", last_updated=day_ago)
        await self._check_with_history(bt, items)
        assert bt.damped_outdoor_temperature == pytest.approx(10.0)

    async def test_a_history_item_without_a_unit_reads_in_the_sensor_unit(self):
        """A recorded state that lost its unit attribute is read in the sensor's unit."""
        day_ago = NOW - timedelta(hours=24)
        item = self._hist_item("50.0", day_ago)
        item.attributes = {}
        bt = make_bt(
            make_hass(
                states={
                    OUTDOOR_ID: make_state(
                        state="50.0",
                        attrs={"unit_of_measurement": UnitOfTemperature.FAHRENHEIT},
                        last_updated=day_ago,
                    )
                },
                components={"recorder"},
            ),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=5.0,
        )
        await self._check_with_history(bt, [item])
        assert bt.damped_outdoor_temperature == pytest.approx(10.0)

    @pytest.mark.parametrize(
        ("first", "expected"),
        [
            pytest.param("12.0", False, id="was_in_summer_mode"),
            pytest.param("8.0", True, id="was_heating"),
            pytest.param("9.5", True, id="always_inside_the_band"),
        ],
    )
    async def test_a_restart_inside_the_band_keeps_the_mode_the_history_reached(
        self, first, expected
    ):
        """The history decides on which side of the band a restart lands.

        A fresh entity starts heating. Every history ends with the damped
        temperature inside the band (9 to 10 °C against 10 °C), already at
        the last recorded reading: one came down from summer mode and stays
        there, one came up from heating and keeps heating, and one never
        left the band and heats, as a room does that starts in it.
        """
        items = [
            self._hist_item(first, NOW - timedelta(hours=72)),
            self._hist_item("9.5", NOW - timedelta(hours=60)),
            self._hist_item("9.6", NOW - timedelta(hours=1)),
        ]
        bt = self._recorder_bt(reading="9.6", last_updated=NOW - timedelta(hours=1))
        assert bt.call_for_heat is True
        await self._check_with_history(bt, items)
        assert 9.0 < bt.damped_outdoor_temperature < 10.0
        assert bt.call_for_heat is expected

    async def test_recorder_malformed_history_is_tolerated(self):
        """A non-dict history payload must not raise.

        It falls back to the current reading instead of disabling the threshold
        (issue #2038).
        """
        bt = self._recorder_bt(reading="5.0")
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value=["not", "a", "dict"]
            )
            await check_ambient_air_temperature(bt)
        assert bt.damped_outdoor_temperature == pytest.approx(5.0)
        assert bt.call_for_heat is True

    async def test_recorder_empty_history_falls_back_to_current_reading(self):
        """An empty recorder history must fall back to the current reading.

        It must not wipe the reading and force heat (issue #2038).
        """
        bt = self._recorder_bt(reading="5.0")
        await self._check_with_history(bt, [])
        assert bt.damped_outdoor_temperature == pytest.approx(5.0)
        assert bt.call_for_heat is True

    async def test_recorder_empty_history_above_threshold_disables_heat(self):
        """Outdoor above the threshold with no usable history disables heating.

        The #2038 symptom: it must still disable heating, not default to heat.
        """
        bt = self._recorder_bt(reading="21.0", off_temperature=14.0)
        await self._check_with_history(bt, [])
        assert bt.damped_outdoor_temperature == pytest.approx(21.0)
        assert bt.call_for_heat is False

    async def test_the_history_is_read_once(self):
        """After the first read the filter runs on the live readings alone."""
        bt = self._recorder_bt(reading="5.0", last_updated=NOW - timedelta(hours=72))
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            query = AsyncMock(return_value={OUTDOOR_ID: []})
            gi.return_value.async_add_executor_job = query
            await check_ambient_air_temperature(bt)
            bt.clock.advance(OUTDOOR_HISTORY_RETRY.total_seconds() * 10)
            bt.hass.states.get = MagicMock(
                return_value=make_state(
                    state="18.0",
                    attrs={"unit_of_measurement": "°C"},
                    last_updated=bt.clock.utcnow(),
                )
            )
            await check_ambient_air_temperature(bt)
        assert query.await_count == 1
        assert bt.outdoor_damping.reading == pytest.approx(18.0)
        assert bt.damped_outdoor_temperature == pytest.approx(5.0)

    def _failing_query(self, *responses):
        return AsyncMock(side_effect=list(responses))

    async def test_history_query_failure_does_not_propagate(self, caplog):
        """A failing recorder query leaves the check running on the live reading.

        The failure is reported once, not on every retry that meets it.
        """
        bt = self._recorder_bt(reading="18.0")
        failure = OperationalError("SELECT", {}, Exception("database is locked"))
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            query = self._failing_query(failure, failure)
            gi.return_value.async_add_executor_job = query
            with caplog.at_level(logging.WARNING, logger=WEATHER_MOD):
                await check_ambient_air_temperature(bt)
                bt.clock.advance(OUTDOOR_HISTORY_RETRY.total_seconds())
                await check_ambient_air_temperature(bt)
        assert query.await_count == 2
        assert bt.damped_outdoor_temperature == pytest.approx(18.0)
        assert bt.call_for_heat is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    async def test_a_failed_read_waits_for_the_retry_interval(self):
        """A check inside the retry interval does not read the recorder again."""
        bt = self._recorder_bt(reading="18.0")
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            query = self._failing_query(RuntimeError("no database"), {OUTDOOR_ID: []})
            gi.return_value.async_add_executor_job = query
            await check_ambient_air_temperature(bt)
            bt.clock.advance(OUTDOOR_HISTORY_RETRY.total_seconds() - 1)
            await check_ambient_air_temperature(bt)
        assert query.await_count == 1
        assert bt.outdoor_history_damped is False

    async def test_a_retried_read_fills_the_filter(self):
        """Once the recorder answers, its history replaces the live-only filter."""
        bt = self._recorder_bt(reading="18.0")
        cold = [self._hist_item("2.0", NOW - timedelta(hours=72))]
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            query = self._failing_query(
                RuntimeError("database connection has not been established"),
                {OUTDOOR_ID: cold},
            )
            gi.return_value.async_add_executor_job = query
            await check_ambient_air_temperature(bt)
            assert bt.call_for_heat is False
            bt.clock.advance(OUTDOOR_HISTORY_RETRY.total_seconds())
            await check_ambient_air_temperature(bt)
        assert query.await_count == 2
        assert bt.outdoor_history_damped is True
        held_seconds = OUTDOOR_HISTORY_RETRY.total_seconds()
        assert bt.damped_outdoor_temperature == pytest.approx(
            2.0 + 16.0 * -math.expm1(-held_seconds / (24 * HOUR_S))
        )
        assert bt.call_for_heat is True

    async def test_a_check_during_the_history_read_waits_for_it(self):
        """A check that starts during the history read queues behind it.

        The read happens once, and both checks end on the damped history.
        """
        bt = self._recorder_bt(reading="20.0")
        cold = [self._hist_item("2.0", NOW - timedelta(hours=72))]
        read_started = asyncio.Event()
        release_read = asyncio.Event()
        calls = 0

        async def query(*_args):
            nonlocal calls
            calls += 1
            read_started.set()
            await release_read.wait()
            return {OUTDOOR_ID: cold}

        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            gi.return_value.async_add_executor_job = query
            reading = asyncio.create_task(check_ambient_air_temperature(bt))
            await read_started.wait()
            overlapping = asyncio.create_task(check_ambient_air_temperature(bt))
            await asyncio.sleep(0)
            assert bt.outdoor_damping is None
            release_read.set()
            await asyncio.gather(reading, overlapping)
        assert calls == 1
        assert bt.damped_outdoor_temperature == pytest.approx(2.0)
        assert bt.call_for_heat is True


# ===========================================================================
# check_weather (orchestration)
# ===========================================================================


class TestCheckWeather:
    """Orchestration of the weather entity and outdoor sensor sources."""

    async def test_no_entities_forces_heat_and_reports_change(self):
        """With neither source configured, heating is forced on."""
        bt = make_bt(make_hass(), weather_entity_id=None, outdoor_sensor_entity_id=None)
        bt.call_for_heat = False
        assert await check_weather(bt) is True
        assert bt.call_for_heat is True

    async def test_weather_only_applies_prediction(self):
        """A weather-only setup applies the prediction result."""
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        bt.call_for_heat = False  # old value
        with patch(
            f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=True)
        ):
            changed = await check_weather(bt)
        assert bt.call_for_heat is True
        assert changed is True

    async def test_weather_only_no_change_returns_false(self):
        """An unchanged call_for_heat reports no change."""
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        bt.call_for_heat = True
        with patch(
            f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=True)
        ):
            changed = await check_weather(bt)
        assert changed is False
        assert bt.call_for_heat is True

    @pytest.mark.parametrize(
        "previous",
        [pytest.param(True, id="heating"), pytest.param(False, id="summer_mode")],
    )
    async def test_no_opinion_keeps_the_previous_decision(self, previous):
        """A prediction without an opinion leaves call_for_heat where it was.

        No temperature is known in that case, so neither the decision nor the
        logbook may change.
        """
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        bt.call_for_heat = previous
        logbook = AsyncMock()
        with (
            patch(
                f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=None)
            ),
            patch(f"{WEATHER_MOD}.async_fire_logbook_entry", logbook),
        ):
            changed = await check_weather(bt)
        assert bt.call_for_heat is previous
        assert changed is False
        logbook.assert_not_awaited()

    async def test_outdoor_available_but_no_cache_still_heats(self):
        """An available sensor with no cache yet still forces heat."""
        states = {OUTDOOR_ID: make_state(state="5.0")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=None,
            off_temperature=10.0,
        )
        bt.call_for_heat = False
        assert await check_weather(bt) is True
        assert bt.call_for_heat is True

    async def test_outdoor_only_cold_calls_for_heat(self):
        """A cold cached outdoor temperature calls for heat."""
        bt = make_bt(
            make_hass(),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=2.0,
            off_temperature=10.0,
        )
        await check_weather(bt)
        assert bt.call_for_heat is True

    async def test_outdoor_only_warm_stops_heat(self):
        """A warm cached outdoor temperature stops heating."""
        bt = make_bt(
            make_hass(),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=18.0,
            off_temperature=10.0,
        )
        await check_weather(bt)
        assert bt.call_for_heat is False

    @pytest.mark.parametrize(
        ("call_for_heat", "damped", "expected"),
        [
            pytest.param(True, 9.5, True, id="heating_inside_the_band"),
            pytest.param(False, 9.5, False, id="summer_mode_inside_the_band"),
            pytest.param(False, 8.9, True, id="summer_mode_below_the_band"),
            pytest.param(True, 10.0, False, id="heating_at_the_threshold"),
        ],
    )
    async def test_the_outdoor_verdict_has_a_hysteresis_band(
        self, call_for_heat, damped, expected
    ):
        """Summer mode starts at the threshold and ends one kelvin below it."""
        bt = make_bt(
            make_hass(),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=damped,
            off_temperature=10.0,
            call_for_heat=call_for_heat,
        )
        await check_weather(bt)
        assert bt.call_for_heat is expected

    async def test_the_hourly_check_advances_a_held_outdoor_reading(self):
        """A sensor that holds still after a cold snap still ends summer mode.

        The sensor reports 0 °C once and then nothing, so no outdoor check
        runs until 05:00. The hourly check brings the damped temperature up
        to now: from 20 °C it falls below the 9 °C band after about 19 hours.
        """
        damping = add_reading(
            start_damping(20.0, NOW.timestamp() - DAY_S), 0.0, NOW.timestamp()
        )
        bt = make_bt(
            make_hass(),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=10.0,
            call_for_heat=False,
            outdoor_damping=damping,
            damped_outdoor_temperature=20.0,
        )
        heating_from = None
        for hour in range(1, 25):
            bt.clock.advance(HOUR_S)
            await check_weather(bt)
            if bt.call_for_heat and heating_from is None:
                heating_from = hour
        assert heating_from == 20
        assert bt.damped_outdoor_temperature == pytest.approx(20.0 * math.exp(-1))

    async def test_outdoor_missing_cache_forces_heat(self):
        """A missing cache with an unavailable sensor forces heat."""
        states = {OUTDOOR_ID: make_state(state="unavailable")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=None,
            off_temperature=10.0,
        )
        await check_weather(bt)
        assert bt.call_for_heat is True

    async def test_outdoor_sensor_overrides_weather_to_heat(self):
        """With both sources, the outdoor sensor's verdict wins outright."""
        bt = make_bt(
            make_hass(),
            weather_entity_id=WEATHER_ID,
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=2.0,  # cold -> heat
            off_temperature=10.0,
        )
        # Weather says "no heat" but it is discarded.
        with patch(
            f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=False)
        ):
            await check_weather(bt)
        assert bt.call_for_heat is True

    async def test_outdoor_sensor_overrides_weather_to_stop(self):
        """The outdoor sensor can override the weather prediction to stop heat."""
        bt = make_bt(
            make_hass(),
            weather_entity_id=WEATHER_ID,
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=18.0,  # warm -> no heat
            off_temperature=10.0,
        )
        pred = AsyncMock(return_value=True)
        with patch(f"{WEATHER_MOD}.check_weather_prediction", pred):
            await check_weather(bt)
        # The outdoor sensor's verdict replaces the weather prediction.
        assert bt.call_for_heat is False

    @pytest.mark.parametrize(
        "previous",
        [pytest.param(True, id="heating"), pytest.param(False, id="summer_mode")],
    )
    @pytest.mark.parametrize(
        "transient",
        [
            "no_forecast_feature",
            "service_error",
            "service_not_supported",
            "empty_forecast",
            "malformed_response",
            "hanging_service",
        ],
    )
    async def test_transient_weather_failure_keeps_the_previous_decision(
        self, transient, previous
    ):
        """A weather entity that cannot answer leaves call_for_heat unchanged.

        Every way the forecast can fail to arrive carries no temperature, so
        it may neither resume nor stop heating, and the logbook stays quiet.
        """
        features = 0 if transient == "no_forecast_feature" else None
        states = {
            WEATHER_ID: weather_state()
            if features is None
            else weather_state(features=features)
        }
        hass = make_hass(states=states)
        if transient == "service_error":
            hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("boom"))
        elif transient == "service_not_supported":
            hass.services.async_call = AsyncMock(
                side_effect=ServiceNotSupported("weather", "get_forecasts", WEATHER_ID)
            )
        elif transient == "empty_forecast":
            hass.services.async_call = AsyncMock(
                return_value={WEATHER_ID: {"forecast": []}}
            )
        elif transient == "malformed_response":
            hass.services.async_call = AsyncMock(return_value=None)
        elif transient == "hanging_service":
            hass.services.async_call = AsyncMock(side_effect=hanging_service_call)
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, outdoor_sensor_entity_id=None)
        bt.call_for_heat = previous
        logbook = AsyncMock()
        with (
            patch(f"{WEATHER_MOD}.async_fire_logbook_entry", logbook),
            patch(f"{WEATHER_MOD}.FORECAST_CALL_TIMEOUT", timedelta(seconds=0.01)),
        ):
            changed = await asyncio.wait_for(check_weather(bt), timeout=5)
        assert bt.call_for_heat is previous
        assert changed is False
        logbook.assert_not_awaited()

    @pytest.mark.parametrize(
        "previous",
        [pytest.param(True, id="heating"), pytest.param(False, id="summer_mode")],
    )
    async def test_missing_off_temperature_keeps_the_previous_decision(self, previous):
        """A weather-only setup without a threshold does not change its decision.

        The forecast cannot be judged without an off_temperature, so the
        room keeps heating, or keeps resting, as it did before.
        """
        states = {WEATHER_ID: weather_state(temperature=25.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [25.0, 25.0])
        )
        bt = make_bt(hass, weather_entity_id=WEATHER_ID, off_temperature=None)
        bt.call_for_heat = previous
        logbook = AsyncMock()
        with patch(f"{WEATHER_MOD}.async_fire_logbook_entry", logbook):
            changed = await check_weather(bt)
        assert bt.call_for_heat is previous
        assert changed is False
        logbook.assert_not_awaited()


HOUR_S = 3600.0
DAY_S = 24 * HOUR_S


async def _hourly_checks(bt, verdicts, clock_steps=None):
    """Run one weather check per hour, answering each with the next verdict.

    ``clock_steps`` optionally maps a check index to a callable run on the
    clock after that check, for time jumps on the wall-clock axis alone.
    """
    prediction = AsyncMock(side_effect=list(verdicts))
    with (
        patch(f"{WEATHER_MOD}.check_weather_prediction", prediction),
        patch(f"{WEATHER_MOD}.async_fire_logbook_entry", AsyncMock()),
    ):
        for index, _ in enumerate(verdicts):
            await check_weather(bt)
            bt.clock.advance(HOUR_S)
            if clock_steps and index in clock_steps:
                clock_steps[index](bt.clock)


def _weather_records(caplog, level):
    """Return the records of one level that name the weather entity."""
    return [
        r for r in caplog.records if r.levelno == level and WEATHER_ID in r.getMessage()
    ]


class TestForecastOutage:
    """How long a weather-only setup holds its decision without a forecast."""

    async def test_a_lasting_outage_resumes_heating(self, caplog):
        """A weather entity that stays silent cannot keep the room in summer mode.

        After a warm forecast the room rests; once the entity has given no
        verdict for longer than the hold, the room heats again, and the
        fallback is announced once, naming the weather entity.
        """
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        with caplog.at_level(logging.WARNING, logger=WEATHER_MOD):
            await _hourly_checks(bt, [False] + [None] * 720)

        assert bt.call_for_heat is True
        assert len(_weather_records(caplog, logging.WARNING)) == 1

    async def test_the_logbook_names_the_missing_forecast_as_the_reason(self):
        """Heating resumed by the fallback is not credited to the outdoor air.

        No outdoor temperature was read on this path, so the logbook entry
        names the missing forecast instead.
        """
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        prediction = AsyncMock(side_effect=[False] + [None] * 5)
        logbook = AsyncMock()
        with (
            patch(f"{WEATHER_MOD}.check_weather_prediction", prediction),
            patch(f"{WEATHER_MOD}.async_fire_logbook_entry", logbook),
        ):
            for _ in range(6):
                await check_weather(bt)
                bt.clock.advance(HOUR_S)

        assert [c.args[1] for c in logbook.await_args_list] == [
            "summer_mode_on",
            "weather_forecast_missing",
        ]

    async def test_a_short_outage_keeps_summer_mode(self):
        """An outage within the hold leaves the room resting."""
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)

        await _hourly_checks(bt, [False, None, None, None])

        assert bt.call_for_heat is False

    async def test_a_returning_verdict_rearms_the_hold(self):
        """A verdict ends the outage, so the next outage gets the full hold again."""
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)

        await _hourly_checks(bt, [False, None, None, False, None, None, None])

        assert bt.call_for_heat is False

    async def test_a_flapping_entity_stays_quiet(self, caplog):
        """An entity missing every other check neither falls back nor logs."""
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        with caplog.at_level(logging.INFO, logger=WEATHER_MOD):
            await _hourly_checks(bt, [False, None] * 24)

        assert bt.call_for_heat is False
        assert _weather_records(caplog, logging.WARNING) == []
        assert _weather_records(caplog, logging.INFO) == []

    async def test_a_verdict_after_the_fallback_is_applied_and_announced(self, caplog):
        """A warm verdict after the fallback restores summer mode, logged once."""
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)
        with caplog.at_level(logging.INFO, logger=WEATHER_MOD):
            await _hourly_checks(bt, [False] + [None] * 5 + [False, False])

        assert bt.call_for_heat is False
        assert len(_weather_records(caplog, logging.WARNING)) == 1
        assert len(_weather_records(caplog, logging.INFO)) == 1

    async def test_the_hold_keeps_its_length_across_a_dst_change(self):
        """A wall clock set back by an hour does not stretch the hold.

        Three hours after the forecast went silent the room heats, even when
        the local clock was turned back in between.
        """
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID)

        def _clock_set_back(clock):
            clock.now_value -= timedelta(hours=1)

        await _hourly_checks(bt, [False] + [None] * 4, clock_steps={1: _clock_set_back})

        assert bt.call_for_heat is True

    @pytest.mark.parametrize(
        ("outdoor_temperature", "expected"),
        [
            pytest.param(25.0, False, id="warm_outside"),
            pytest.param(2.0, True, id="cold_outside"),
        ],
    )
    async def test_the_outdoor_sensor_decides_during_a_forecast_outage(
        self, caplog, outdoor_temperature, expected
    ):
        """With an outdoor sensor configured its reading decides, silently.

        The outdoor sensor's verdict replaces the forecast's, so a silent
        forecast neither forces heating nor announces a fallback.
        """
        bt = make_bt(
            make_hass(),
            weather_entity_id=WEATHER_ID,
            outdoor_sensor_entity_id=OUTDOOR_ID,
            damped_outdoor_temperature=outdoor_temperature,
            off_temperature=10.0,
        )
        with caplog.at_level(logging.INFO, logger=WEATHER_MOD):
            await _hourly_checks(bt, [False] + [None] * 24)

        assert bt.call_for_heat is expected
        assert _weather_records(caplog, logging.WARNING) == []
        assert _weather_records(caplog, logging.INFO) == []


# ===========================================================================
# summer_mode_facts (diagnostics)
# ===========================================================================


class TestSummerModeFacts:
    """What the diagnostics download reports about the summer-mode decision."""

    def test_a_room_in_summer_mode_reports_the_lowered_threshold(self):
        bt = make_bt(
            make_hass(),
            outdoor_sensor_entity_id=OUTDOOR_ID,
            off_temperature=18.0,
            call_for_heat=False,
            damped_outdoor_temperature=17.4,
            outdoor_damping=start_damping(22.0, NOW.timestamp()),
            outdoor_history_damped=True,
        )
        assert summer_mode_facts(bt) == {
            "call_for_heat": False,
            "off_temperature": 18.0,
            "heat_threshold": 17.0,
            "damped_outdoor_temperature": 17.4,
            "outdoor_reading": 22.0,
            "outdoor_reading_at": "2025-01-01T12:00:00+00:00",
            "outdoor_history_damped": True,
            "outdoor_history_failing": False,
            "weather_fallback_active": False,
        }

    def test_a_room_without_readings_or_threshold_reports_none(self):
        bt = make_bt(make_hass(), weather_entity_id=WEATHER_ID, off_temperature=None)
        facts = summer_mode_facts(bt)
        assert facts["heat_threshold"] is None
        assert facts["outdoor_reading"] is None
        assert facts["outdoor_reading_at"] is None
        assert facts["damped_outdoor_temperature"] is None
