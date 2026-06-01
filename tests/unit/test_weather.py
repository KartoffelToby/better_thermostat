"""Tests for utils/weather.py — call-for-heat decisions from weather data.

The module has three public coroutines plus a helper class:

* ``check_weather`` — orchestrates the call-for-heat decision from a weather
  entity and/or an outdoor sensor.
* ``check_weather_prediction`` — evaluates a weather entity's forecast.
* ``check_ambient_air_temperature`` — evaluates an outdoor sensor (and its
  recorder history when available).
* ``DailyHistory`` — accumulates per-day means and exposes a multi-day mean
  via the ``min`` attribute.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.weather import WeatherEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported
import pytest

from custom_components.better_thermostat.utils.weather import (
    DailyHistory,
    check_ambient_air_temperature,
    check_weather,
    check_weather_prediction,
)

WEATHER_ID = "weather.home"
OUTDOOR_ID = "sensor.outdoor"

WEATHER_MOD = "custom_components.better_thermostat.utils.weather"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_state(state="20.0", attrs=None):
    """Build a minimal stand-in for a HA State with state and attributes."""
    s = MagicMock()
    s.state = state
    s.attributes = attrs if attrs is not None else {}
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
        device_name="Test BT",
        weather_entity=None,
        outdoor_sensor=None,
        off_temperature=10.0,
        last_avg_outdoor_temp=None,
        call_for_heat=True,
    )
    for k, v in kw.items():
        setattr(bt, k, v)
    return bt


def weather_state(
    features=int(WeatherEntityFeature.FORECAST_DAILY), temperature=20.0, unit="°C"
):
    """Build a weather entity state advertising forecast support and a temp."""
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
# DailyHistory — pure, deterministic, the densest place to hunt for bugs
# ===========================================================================


class TestDailyHistory:
    """The legacy ``min`` field now holds a multi-day mean of per-day means."""

    D1 = datetime(2024, 1, 1, 12, 0)
    D2 = datetime(2024, 1, 2, 12, 0)
    D3 = datetime(2024, 1, 3, 12, 0)

    def test_starts_empty(self):
        """A fresh history has no computed mean."""
        assert DailyHistory(2).min is None

    def test_single_measurement_is_its_own_mean(self):
        """A lone reading is reported verbatim as the mean."""
        h = DailyHistory(2)
        h.add_measurement(10.0, self.D1)
        assert h.min == 10.0

    def test_same_day_measurements_are_averaged(self):
        """Two readings on one day average into that day's mean."""
        h = DailyHistory(2)
        h.add_measurement(10.0, self.D1)
        h.add_measurement(20.0, self.D1)
        assert h.min == 15.0

    def test_two_days_one_reading_each(self):
        """Two single-reading days average their per-day means."""
        h = DailyHistory(2)
        h.add_measurement(10.0, self.D1)
        h.add_measurement(30.0, self.D2)
        # mean of per-day means: (10 + 30) / 2
        assert h.min == 20.0

    def test_per_day_mean_then_cross_day_mean(self):
        """Per-day means are computed first, then averaged across days."""
        h = DailyHistory(2)
        h.add_measurement(10.0, self.D1)
        h.add_measurement(20.0, self.D1)  # day1 mean = 15
        h.add_measurement(30.0, self.D2)  # day2 mean = 30
        assert h.min == pytest.approx((15.0 + 30.0) / 2)

    def test_eviction_drops_oldest_day(self):
        """A third day evicts the oldest beyond the two-day window."""
        h = DailyHistory(2)
        h.add_measurement(10.0, self.D1)
        h.add_measurement(20.0, self.D2)
        h.add_measurement(30.0, self.D3)  # evicts D1
        # only D2 and D3 remain: (20 + 30) / 2
        assert h.min == pytest.approx(25.0)

    def test_eviction_cleans_aggregates_no_stale_contribution(self):
        """Evicting a day removes its sum/count so no value leaks forward."""
        h = DailyHistory(2)
        # Pile several readings on D1 so a leaked aggregate would skew the mean.
        for v in (100.0, 100.0, 100.0):
            h.add_measurement(v, self.D1)
        h.add_measurement(20.0, self.D2)
        h.add_measurement(30.0, self.D3)  # D1 must be fully gone
        assert h.min == pytest.approx(25.0)
        assert self.D1.date() not in h._sum_dict
        assert self.D1.date() not in h._count_dict

    def test_out_of_order_measurement_is_skipped(self):
        """A reading older than the current day is dropped."""
        h = DailyHistory(2)
        h.add_measurement(30.0, self.D2)
        h.add_measurement(10.0, self.D1)  # earlier than current day -> dropped
        assert h.min == 30.0

    def test_non_numeric_value_ignored(self):
        """Non-numeric readings do not affect the mean."""
        h = DailyHistory(2)
        h.add_measurement(10.0, self.D1)
        h.add_measurement("warm", self.D1)
        h.add_measurement(None, self.D1)
        assert h.min == 10.0

    def test_first_value_non_numeric_keeps_history_empty(self):
        """A non-numeric first reading leaves the history uninitialised."""
        h = DailyHistory(2)
        h.add_measurement(None, self.D1)
        assert h.min is None
        assert h._days is None

    def test_timestamp_defaults_to_now(self):
        """With no timestamp the measurement is filed under today."""
        fixed = datetime(2024, 6, 1, 9, 0)
        with patch(f"{WEATHER_MOD}.dt_util.now", return_value=fixed):
            h = DailyHistory(2)
            h.add_measurement(12.0)
        assert h.min == 12.0
        assert fixed.date() in h._sum_dict


# ===========================================================================
# check_weather_prediction
# ===========================================================================


class TestCheckWeatherPrediction:
    """Forecast evaluation: a missing entity, missing config, and forecasts."""

    async def test_no_weather_entity_returns_false(self):
        """Without a weather entity the prediction is False."""
        bt = make_bt(make_hass(), weather_entity=None)
        assert await check_weather_prediction(bt) is False

    async def test_off_temperature_none_returns_false(self):
        """A missing off_temperature short-circuits to False."""
        bt = make_bt(make_hass(), weather_entity=WEATHER_ID, off_temperature=None)
        assert await check_weather_prediction(bt) is False

    async def test_no_forecast_support_returns_none(self):
        """An entity without any forecast feature yields None (no opinion)."""
        states = {WEATHER_ID: weather_state(features=0)}
        bt = make_bt(make_hass(states=states), weather_entity=WEATHER_ID)
        assert await check_weather_prediction(bt) is None

    async def test_cold_forecast_calls_for_heat(self):
        """A forecast below the threshold calls for heat."""
        states = {WEATHER_ID: weather_state(temperature=2.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [1.0, 1.0])
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_warm_forecast_and_warm_now_no_heat(self):
        """A warm forecast and warm current temp do not call for heat."""
        states = {WEATHER_ID: weather_state(temperature=18.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [17.0, 16.0])
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is False

    async def test_current_temp_below_off_drives_heat_even_if_forecast_warm(self):
        """A cold current temperature alone is enough to call for heat."""
        states = {WEATHER_ID: weather_state(temperature=2.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [20.0, 20.0])
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
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
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_empty_forecast_returns_false(self):
        """An empty forecast list resolves to False."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value={WEATHER_ID: {"forecast": []}}
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID)
        assert await check_weather_prediction(bt) is False

    async def test_service_error_returns_false(self):
        """A HomeAssistantError from the service resolves to False."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("boom"))
        bt = make_bt(hass, weather_entity=WEATHER_ID)
        assert await check_weather_prediction(bt) is False

    async def test_service_not_supported_returns_false(self):
        """A ServiceNotSupported error resolves to False."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            side_effect=ServiceNotSupported("weather", "get_forecasts", WEATHER_ID)
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID)
        assert await check_weather_prediction(bt) is False

    async def test_forecast_temps_are_averaged(self):
        """Up to two forecast temps are averaged before the comparison.

        Forecast = [15, 1] with off_temperature 10 and a warm current temp:
        the mean (8) is below the threshold, so heating is requested.
        """
        states = {WEATHER_ID: weather_state(temperature=15.0)}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [15.0, 1.0])
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_daily_forecast_samples_two_entries(self):
        """A daily forecast averages the first two entries (~two days)."""
        states = {WEATHER_ID: weather_state(temperature=15.0)}
        hass = make_hass(states=states)
        # First two warm, later days freezing -> beyond the two-day horizon.
        hass.services.async_call = AsyncMock(
            return_value=forecast_resp(WEATHER_ID, [15.0, 15.0, -30.0, -30.0])
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
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
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

    async def test_forecast_entry_missing_temperature_is_filtered(self):
        """A forecast entry without a temperature is filtered out."""
        states = {WEATHER_ID: weather_state(temperature=15.0)}
        hass = make_hass(states=states)
        # First entry has no temperature -> filtered; second is freezing.
        hass.services.async_call = AsyncMock(
            return_value={WEATHER_ID: {"forecast": [{"foo": 1}, {"temperature": 1.0}]}}
        )
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
        assert await check_weather_prediction(bt) is True

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
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
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
        bt = make_bt(hass, weather_entity=WEATHER_ID, off_temperature=10.0)
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
        bt = make_bt(make_hass(), outdoor_sensor=None)
        assert await check_ambient_air_temperature(bt) is None

    async def test_off_temperature_not_float_returns_none(self):
        """A missing off_temperature short-circuits to None."""
        bt = make_bt(make_hass(), outdoor_sensor=OUTDOOR_ID, off_temperature=None)
        assert await check_ambient_air_temperature(bt) is None

    async def test_unavailable_sensor_without_cache_forces_heat(self):
        """An unavailable sensor with no cached value forces heating on."""
        states = {OUTDOOR_ID: make_state(state="unavailable")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=None,
            call_for_heat=False,
        )
        assert await check_ambient_air_temperature(bt) is None
        assert bt.call_for_heat is True

    async def test_unavailable_sensor_with_cache_keeps_state(self):
        """An unavailable sensor with a cached value leaves call_for_heat alone."""
        states = {OUTDOOR_ID: make_state(state="unknown")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=8.0,
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
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=None,
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
            outdoor_sensor=OUTDOOR_ID,
            off_temperature=10.0,
        )
        await check_ambient_air_temperature(bt)
        assert bt.call_for_heat is True
        assert bt.last_avg_outdoor_temp == 5.0

    async def test_no_recorder_uses_current_reading_warm(self):
        """Without recorder, a warm current reading stops heating."""
        states = {
            OUTDOOR_ID: make_state(state="18.0", attrs={"unit_of_measurement": "°C"})
        }
        bt = make_bt(
            make_hass(states=states, components=set()),
            outdoor_sensor=OUTDOOR_ID,
            off_temperature=10.0,
        )
        await check_ambient_air_temperature(bt)
        assert bt.call_for_heat is False
        assert bt.last_avg_outdoor_temp == 18.0

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
            outdoor_sensor=OUTDOOR_ID,
            off_temperature=5.0,
        )
        await check_ambient_air_temperature(bt)
        # 50 °F == 10 °C, above a 5 °C threshold -> no heat.
        assert bt.last_avg_outdoor_temp == pytest.approx(10.0)
        assert bt.call_for_heat is False

    def _hist_item(self, state, ts, unit="°C"):
        """Build a recorder history item stand-in."""
        it = MagicMock()
        it.state = state
        it.last_updated = ts
        it.attributes = {"unit_of_measurement": unit}
        return it

    async def test_recorder_history_computes_multi_day_mean(self):
        """With recorder, the multi-day mean drives the heat decision."""
        states = {
            OUTDOOR_ID: make_state(state="5.0", attrs={"unit_of_measurement": "°C"})
        }
        hass = make_hass(states=states, components={"recorder"})
        day1, day2 = datetime(2024, 1, 1, 12), datetime(2024, 1, 2, 12)
        items = [
            self._hist_item("10.0", day1),
            self._hist_item("20.0", day1),  # day1 mean = 15
            self._hist_item("30.0", day2),  # day2 mean = 30
        ]
        bt = make_bt(hass, outdoor_sensor=OUTDOOR_ID, off_temperature=10.0)
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value={OUTDOOR_ID: items}
            )
            await check_ambient_air_temperature(bt)
        # (15 + 30) / 2 = 22.5, above threshold -> no heat.
        assert bt.last_avg_outdoor_temp == pytest.approx(22.5)
        assert bt.call_for_heat is False

    async def test_recorder_history_filters_bad_states(self):
        """Unknown/unavailable/non-numeric history states are filtered out."""
        states = {
            OUTDOOR_ID: make_state(state="5.0", attrs={"unit_of_measurement": "°C"})
        }
        hass = make_hass(states=states, components={"recorder"})
        day1 = datetime(2024, 1, 1, 12)
        items = [
            self._hist_item("unavailable", day1),
            self._hist_item("unknown", day1),
            self._hist_item("not-a-number", day1),
            self._hist_item("4.0", day1),  # the only usable reading
        ]
        bt = make_bt(hass, outdoor_sensor=OUTDOOR_ID, off_temperature=10.0)
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value={OUTDOOR_ID: items}
            )
            await check_ambient_air_temperature(bt)
        assert bt.last_avg_outdoor_temp == pytest.approx(4.0)
        assert bt.call_for_heat is True

    async def test_recorder_malformed_history_is_tolerated(self):
        """A non-dict history payload must not raise; it yields no data."""
        states = {
            OUTDOOR_ID: make_state(state="5.0", attrs={"unit_of_measurement": "°C"})
        }
        hass = make_hass(states=states, components={"recorder"})
        bt = make_bt(hass, outdoor_sensor=OUTDOOR_ID, off_temperature=10.0)
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value=["not", "a", "dict"]
            )
            await check_ambient_air_temperature(bt)
        assert bt.last_avg_outdoor_temp is None
        assert bt.call_for_heat is True

    async def test_recorder_empty_history_yields_none_and_forces_heat(self):
        """An empty recorder history leaves no average and forces heat."""
        states = {
            OUTDOOR_ID: make_state(state="5.0", attrs={"unit_of_measurement": "°C"})
        }
        hass = make_hass(states=states, components={"recorder"})
        bt = make_bt(hass, outdoor_sensor=OUTDOOR_ID, off_temperature=10.0)
        with patch(f"{WEATHER_MOD}.get_instance") as gi:
            gi.return_value.async_add_executor_job = AsyncMock(
                return_value={OUTDOOR_ID: []}
            )
            await check_ambient_air_temperature(bt)
        assert bt.last_avg_outdoor_temp is None
        assert bt.call_for_heat is True


# ===========================================================================
# check_weather (orchestration)
# ===========================================================================


class TestCheckWeather:
    """Orchestration of the weather entity and outdoor sensor sources."""

    async def test_no_entities_forces_heat_and_reports_change(self):
        """With neither source configured, heating is forced on."""
        bt = make_bt(make_hass(), weather_entity=None, outdoor_sensor=None)
        bt.call_for_heat = False
        assert await check_weather(bt) is True
        assert bt.call_for_heat is True

    async def test_weather_only_applies_prediction(self):
        """A weather-only setup applies the prediction result."""
        bt = make_bt(make_hass(), weather_entity=WEATHER_ID)
        bt.call_for_heat = False  # old value
        with patch(
            f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=True)
        ):
            changed = await check_weather(bt)
        assert bt.call_for_heat is True
        assert changed is True

    async def test_weather_only_no_change_returns_false(self):
        """An unchanged call_for_heat reports no change."""
        bt = make_bt(make_hass(), weather_entity=WEATHER_ID)
        bt.call_for_heat = True
        with patch(
            f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=True)
        ):
            changed = await check_weather(bt)
        assert changed is False
        assert bt.call_for_heat is True

    async def test_weather_none_response_keeps_default_heat(self):
        """A None prediction is not applied; the default heat wins."""
        bt = make_bt(make_hass(), weather_entity=WEATHER_ID)
        bt.call_for_heat = False
        with patch(
            f"{WEATHER_MOD}.check_weather_prediction", AsyncMock(return_value=None)
        ):
            await check_weather(bt)
        # None is not applied; the default (True) set at the top wins.
        assert bt.call_for_heat is True

    async def test_outdoor_available_but_no_cache_still_heats(self):
        """An available sensor with no cache yet still forces heat."""
        states = {OUTDOOR_ID: make_state(state="5.0")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=None,
            off_temperature=10.0,
        )
        bt.call_for_heat = False
        assert await check_weather(bt) is True
        assert bt.call_for_heat is True

    async def test_outdoor_only_cold_calls_for_heat(self):
        """A cold cached outdoor temperature calls for heat."""
        bt = make_bt(
            make_hass(),
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=2.0,
            off_temperature=10.0,
        )
        await check_weather(bt)
        assert bt.call_for_heat is True

    async def test_outdoor_only_warm_stops_heat(self):
        """A warm cached outdoor temperature stops heating."""
        bt = make_bt(
            make_hass(),
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=18.0,
            off_temperature=10.0,
        )
        await check_weather(bt)
        assert bt.call_for_heat is False

    async def test_outdoor_missing_cache_forces_heat(self):
        """A missing cache with an unavailable sensor forces heat."""
        states = {OUTDOOR_ID: make_state(state="unavailable")}
        bt = make_bt(
            make_hass(states=states),
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=None,
            off_temperature=10.0,
        )
        await check_weather(bt)
        assert bt.call_for_heat is True

    async def test_outdoor_sensor_overrides_weather_to_heat(self):
        """With both sources, the outdoor sensor's verdict wins outright."""
        bt = make_bt(
            make_hass(),
            weather_entity=WEATHER_ID,
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=2.0,  # cold -> heat
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
            weather_entity=WEATHER_ID,
            outdoor_sensor=OUTDOOR_ID,
            last_avg_outdoor_temp=18.0,  # warm -> no heat
            off_temperature=10.0,
        )
        pred = AsyncMock(return_value=True)
        with patch(f"{WEATHER_MOD}.check_weather_prediction", pred):
            await check_weather(bt)
        # The outdoor sensor's verdict replaces the weather prediction.
        assert bt.call_for_heat is False


# ===========================================================================
# Behaviour the current implementation does not yet provide (xfail).
# ===========================================================================


class TestSuspectedBugs:
    """Assertions of intended behaviour the implementation does not yet meet.

    These are marked strict xfail; a strict XPASS signals the behaviour now
    holds and the test should become a plain assertion.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="check_weather_prediction returns False on a transient service "
        "failure, so check_weather sets call_for_heat to False.",
    )
    async def test_transient_weather_failure_should_not_stop_heat(self):
        """A transient weather-service failure leaves heating enabled."""
        states = {WEATHER_ID: weather_state()}
        hass = make_hass(states=states)
        hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("boom"))
        bt = make_bt(hass, weather_entity=WEATHER_ID, outdoor_sensor=None)
        bt.call_for_heat = True
        await check_weather(bt)
        assert bt.call_for_heat is True
