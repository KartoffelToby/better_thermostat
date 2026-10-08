"""Weather utils."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from typing import TYPE_CHECKING

from homeassistant.components.recorder import history
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    WeatherEntityFeature,
)
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported

# get_instance location can differ between HA versions; prefer helpers API.
from homeassistant.helpers.recorder import get_instance
from sqlalchemy.exc import SQLAlchemyError

from ..core.outdoor import (
    DampedOutdoorTemperature,
    add_reading,
    damped_value_at,
    heat_threshold,
    start_damping,
)
from .helpers import async_fire_logbook_entry, convert_to_float_celsius

if TYPE_CHECKING:
    from custom_components.better_thermostat.climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

# How long a weather-only setup keeps its decision while the weather entity
# gives no forecast verdict. check_weather runs once an hour, so this holds
# the decision across three silent checks: long enough to ride out a cloud
# weather service that is rate-limited or reconnecting, short enough that an
# entity gone for good cannot keep a room in summer mode, which is the side
# the outdoor sensor path also falls back to when it has no data.
WEATHER_VERDICT_HOLD = timedelta(hours=3)

# How much of the outdoor sensor's recorder history fills the damped outdoor
# temperature. Three time constants back, the oldest reading keeps a 5 % share
# of the result.
OUTDOOR_HISTORY_WINDOW = timedelta(hours=72)

# How long check_ambient_air_temperature waits before it reads the recorder
# again after a failed read. Until a read succeeds, the filter runs on the live
# readings alone.
OUTDOOR_HISTORY_RETRY = timedelta(minutes=15)

# How long the get_forecasts service call may take. Coordinator-based weather
# entities answer from memory; one that fetches on demand answers within a
# few seconds while its service is reachable. A cloud integration can instead
# stall until its HTTP client gives up, minutes later, when the internet is
# down, and startup awaits this call before the entity becomes available. A
# call cut off here reads as a missing forecast and falls under
# WEATHER_VERDICT_HOLD like any other.
FORECAST_CALL_TIMEOUT = timedelta(seconds=10)


async def check_weather(self: BetterThermostat) -> bool:
    """Check weather predictions or ambient air temperature if available.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    bool
            true if call_for_heat was changed
    """
    old_call_for_heat = self.call_for_heat
    _outdoor_heat_threshold = (
        None
        if self.off_temperature is None
        else heat_threshold(self.off_temperature, old_call_for_heat)
    )
    _call_for_heat_weather: bool | None = None
    _call_for_heat_outdoor = False

    if self.weather_entity_id is not None:
        _call_for_heat_weather = await check_weather_prediction(self)
        if isinstance(_call_for_heat_weather, bool):
            if self.weather_fallback_active:
                _LOGGER.info(
                    "better_thermostat %s: weather entity %s gives a forecast "
                    "verdict again, heating follows the forecast",
                    self.device_name,
                    self.weather_entity_id,
                )
            self.weather_verdict_missing_since = None
            self.weather_fallback_active = False
            self.call_for_heat = _call_for_heat_weather
        elif self.outdoor_sensor_entity_id is None:
            # None means the prediction has no opinion: the previous decision
            # stays for WEATHER_VERDICT_HOLD, then the room heats. With an
            # outdoor sensor configured its verdict decides below, so the
            # hold only applies where the forecast is the only source.
            # Monotonic time keeps the hold its length across a DST change.
            _now = self.clock.monotonic()
            if self.weather_verdict_missing_since is None:
                self.weather_verdict_missing_since = _now
            _silent_s = _now - self.weather_verdict_missing_since
            if (
                not self.weather_fallback_active
                and _silent_s >= WEATHER_VERDICT_HOLD.total_seconds()
            ):
                _LOGGER.warning(
                    "better_thermostat %s: weather entity %s has given no forecast "
                    "for %.1f hours, resuming heating until it does",
                    self.device_name,
                    self.weather_entity_id,
                    _silent_s / 3600.0,
                )
                self.weather_fallback_active = True
            if self.weather_fallback_active:
                self.call_for_heat = True

    if self.outdoor_sensor_entity_id is not None:
        if (
            self.outdoor_source.damping is not None
            and self.damped_outdoor_temperature is not None
        ):
            # The held reading keeps pulling the damped temperature between
            # sensor reports. A sensor that holds still reports nothing, so
            # bring the value up to now instead of deciding on the one the
            # last report left.
            self.damped_outdoor_temperature = damped_value_at(
                self.outdoor_source.damping, self.clock.utcnow().timestamp()
            )
        if self.damped_outdoor_temperature is None or _outdoor_heat_threshold is None:
            # Check if sensor is currently unavailable (expected during startup)
            _outdoor_state = self.hass.states.get(self.outdoor_sensor_entity_id)
            _sensor_unavailable = _outdoor_state is None or _outdoor_state.state in (
                "unavailable",
                "unknown",
                None,
            )

            if _sensor_unavailable:
                # Sensor not ready yet - expected during startup, just debug
                _LOGGER.debug(
                    "better_thermostat %s: outdoor sensor not yet available, fallback to heat",
                    self.device_name,
                )
            else:
                # Sensor is available but we have no cached data - unexpected, warn
                _LOGGER.warning(
                    "better_thermostat %s: outdoor sensor available but no data cached, fallback to heat",
                    self.device_name,
                )
            _call_for_heat_outdoor = True
        else:
            _call_for_heat_outdoor = (
                self.damped_outdoor_temperature < _outdoor_heat_threshold
            )

        self.call_for_heat = _call_for_heat_outdoor

    if self.weather_entity_id is None and self.outdoor_sensor_entity_id is None:
        self.call_for_heat = True

    if old_call_for_heat != self.call_for_heat:
        if not self.call_for_heat:
            await async_fire_logbook_entry(
                self,
                "summer_mode_on",
                "turned off because the outdoor temperature is too high",
            )
        elif self.weather_fallback_active:
            await async_fire_logbook_entry(
                self,
                "weather_forecast_missing",
                "resumed heating because the weather forecast is unavailable",
            )
        else:
            await async_fire_logbook_entry(
                self,
                "summer_mode_off",
                "resumed heating because the outdoor temperature dropped",
            )

    return old_call_for_heat != self.call_for_heat


async def check_weather_prediction(self: BetterThermostat) -> bool | None:
    """Check configured weather entity over roughly the next two days.

    The forecast horizon is normalised to about two days regardless of the
    entity's forecast granularity (daily, twice-daily or hourly), and the
    sampled temperatures are averaged.

    Returns
    -------
    bool
            True if the average forecast temperature (or the current
            temperature) is below the heat threshold, i.e. heat is required;
            the threshold is the off temperature while the room heats and
            the hysteresis band below it while the room is in summer mode
    None
            if not successful
    """
    if self.weather_entity_id is None:
        _LOGGER.warning(
            "better_thermostat %s: weather entity not available.", self.device_name
        )
        return False

    if self.off_temperature is None or not isinstance(self.off_temperature, float):
        _LOGGER.warning(
            "better_thermostat %s: off_temperature not set or not a float.",
            self.device_name,
        )
        return None

    try:
        state = self.hass.states.get(self.weather_entity_id)
        features = state.attributes.get("supported_features", 0) if state else 0
        if state is None or not features & (
            WeatherEntityFeature.FORECAST_DAILY
            | WeatherEntityFeature.FORECAST_TWICE_DAILY
            | WeatherEntityFeature.FORECAST_HOURLY
        ):
            _LOGGER.warning(
                "better_thermostat %s: weather entity '%s' does not advertise any forecast support.",
                self.device_name,
                self.weather_entity_id,
            )
            return None
        if features & WeatherEntityFeature.FORECAST_DAILY:
            ftype = "daily"
        elif features & WeatherEntityFeature.FORECAST_TWICE_DAILY:
            ftype = "twice_daily"
        else:
            ftype = "hourly"

        # Sample roughly the next two days regardless of forecast granularity.
        _forecast_samples = {"daily": 2, "twice_daily": 4, "hourly": 48}[ftype]

        try:
            async with asyncio.timeout(FORECAST_CALL_TIMEOUT.total_seconds()):
                forecasts = await self.hass.services.async_call(
                    WEATHER_DOMAIN,
                    "get_forecasts",
                    {"type": ftype, "entity_id": [self.weather_entity_id]},
                    blocking=True,
                    return_response=True,
                )
        except TimeoutError:
            _LOGGER.warning(
                "better_thermostat %s: weather entity %s did not return a "
                "forecast within %.0f seconds",
                self.device_name,
                self.weather_entity_id,
                FORECAST_CALL_TIMEOUT.total_seconds(),
            )
            return None
        forecast_container = (
            forecasts.get(self.weather_entity_id)
            if isinstance(forecasts, dict)
            else None
        )
        forecast = (
            forecast_container.get("forecast")
            if isinstance(forecast_container, dict)
            else None
        )
        if isinstance(forecast, list) and len(forecast) > 0:
            entity_temperature_unit = state.attributes.get("temperature_unit")
            temps = [
                _forecast_entry_temperature(self, entry, entity_temperature_unit)
                for entry in forecast[:_forecast_samples]
            ]
            valid_temps: list[float] = [t for t in temps if t is not None]
            forecast_mean = None
            if valid_temps:
                forecast_mean = sum(valid_temps) / float(len(valid_temps))
            self.forecast_temperature = forecast_mean

            async with outdoor_check_lock(self):
                damped_current = await _damp_live_reading(
                    self,
                    self.weather_source,
                    self.weather_entity_id,
                    state,
                    partial(_weather_reading, self, entity_temperature_unit),
                    attribute_changes=True,
                    decides=self.outdoor_sensor_entity_id is None,
                )
            self.damped_weather_temperature = damped_current

            # A forecast whose entries and current reading are all unusable
            # carries no temperature at all, so it gives no opinion rather
            # than the "warm" an empty comparison would read as.
            if forecast_mean is None and damped_current is None:
                return None
            threshold = heat_threshold(self.off_temperature, self.call_for_heat)
            _LOGGER.debug(
                "better_thermostat %s: damped weather temperature: %s, forecast "
                "mean: %s, heating below %.2f",
                self.device_name,
                damped_current,
                forecast_mean,
                threshold,
            )
            cond_cur = damped_current is not None and damped_current < threshold
            cond_fc = forecast_mean is not None and forecast_mean < threshold
            return bool(cond_cur or cond_fc)
        else:
            raise TypeError
    except TypeError, ServiceNotSupported, HomeAssistantError:
        _LOGGER.warning(
            "better_thermostat %s: no weather entity data found.", self.device_name
        )
        # Return None (no opinion) on a transient failure so check_weather keeps
        # the current call_for_heat decision while weather data is unavailable.
        return None


@dataclass(slots=True)
class DampedSource:
    """The damped temperature of one outdoor source, and how it was filled.

    ``damping`` is the filter state. ``history_damped`` is set once the
    recorder history has been read into it; until then a failed read is
    retried after ``OUTDOOR_HISTORY_RETRY``, counted from
    ``history_read_at`` on the monotonic clock. ``history_failing`` keeps
    repeated failures out of the warning log.
    """

    damping: DampedOutdoorTemperature | None = None
    history_damped: bool = False
    history_read_at: float | None = None
    history_failing: bool = False


def _numeric(value: object) -> str | int | float | None:
    """Return ``value`` if it can be a temperature reading, else None."""
    return value if isinstance(value, (str, int, float)) else None


def _weather_reading(
    self: BetterThermostat, entity_unit: str | None, state: State
) -> float | None:
    """Return the current temperature a weather entity's state carries, in °C."""
    unit = state.attributes.get("temperature_unit")
    return convert_to_float_celsius(
        _numeric(state.attributes.get("temperature")),
        self.device_name,
        "check_weather_prediction()",
        unit_of_measurement=unit if isinstance(unit, str) else entity_unit,
    )


def _forecast_entry_temperature(
    self: BetterThermostat, entry: object, entity_unit: str | None
) -> float | None:
    """Return the mean temperature of one forecast entry, in °C.

    A daily entry carries the day's high as ``temperature`` and its low as
    ``templow``; the mean of the two stands for the day. An entry without a
    low is taken at its ``temperature``.
    """
    if not isinstance(entry, dict):
        return None
    unit = entry.get("temperature_unit")
    unit = unit if isinstance(unit, str) else entity_unit
    high, low = (
        convert_to_float_celsius(
            _numeric(entry.get(key)),
            self.device_name,
            "check_weather_prediction()",
            unit_of_measurement=unit,
        )
        for key in ("temperature", "templow")
    )
    if high is None:
        return None
    return high if low is None else (high + low) / 2.0


def outdoor_check_lock(self: BetterThermostat) -> asyncio.Lock:
    """Return the lock that serialises filling this entity's damped temperatures.

    The first check of a source suspends while it reads the recorder history
    into the damped temperature. The periodic tick, the outdoor sensor
    listener and the hourly weather check run in their own tasks. Without
    the lock a second check arriving during that read would find no history
    yet, start the filter from its live reading, and read the recorder a
    second time. The lock is created on first use and lives on the entity,
    so each Better Thermostat only queues behind itself.

    Parameters
    ----------
    self :
            self instance of better_thermostat

    Returns
    -------
    asyncio.Lock
            the entity's own lock, created on first use
    """
    lock = self._outdoor_check_lock
    if lock is None:
        lock = asyncio.Lock()
        self._outdoor_check_lock = lock
    return lock


async def _damp_live_reading(
    self: BetterThermostat,
    source: DampedSource,
    entity_id: str,
    current: State,
    reading_of: Callable[[State], float | None],
    *,
    attribute_changes: bool,
    decides: bool,
) -> float | None:
    """Fill ``source`` from history once, add the current reading, return the value.

    ``reading_of`` turns a state of ``entity_id`` into °C. With ``decides``
    the summer-mode decision the history reached replaces ``call_for_heat``.
    Returns the damped temperature now, or None without any usable reading.
    Callers hold :func:`outdoor_check_lock`.
    """
    if (
        not source.history_damped
        and self.off_temperature is not None
        and "recorder" in self.hass.config.components
    ):
        _now = self.clock.monotonic()
        if (
            source.history_read_at is None
            or _now - source.history_read_at >= OUTDOOR_HISTORY_RETRY.total_seconds()
        ):
            source.history_read_at = _now
            try:
                damping, history_call_for_heat = await _damp_history(
                    self,
                    entity_id,
                    reading_of,
                    self.off_temperature,
                    attribute_changes=attribute_changes,
                )
            except SQLAlchemyError, RuntimeError, HomeAssistantError, OSError:
                # The recorder logs the traceback itself. Warn once per run of
                # failures; repeats go to the debug log.
                _LOGGER.log(
                    logging.DEBUG if source.history_failing else logging.WARNING,
                    "better_thermostat %s: reading the history of %s from the "
                    "recorder failed, damping the live readings only",
                    self.device_name,
                    entity_id,
                )
                source.history_failing = True
            else:
                source.history_failing = False
                source.history_damped = True
                if damping is not None:
                    source.damping = damping
                    if decides:
                        self.call_for_heat = history_call_for_heat

    reading = reading_of(current)
    if reading is not None:
        source.damping = add_reading(
            source.damping, reading, current.last_updated.timestamp()
        )
    if source.damping is None:
        return None
    return damped_value_at(source.damping, self.clock.utcnow().timestamp())


async def check_ambient_air_temperature(self: BetterThermostat) -> None:
    """Feed the outdoor reading into the damped temperature and decide on it.

    The first check fills the filter from the recorder's history of the
    outdoor sensor (see :func:`_damp_history`); every later check adds the
    sensor's current reading. Without usable history the filter starts at
    the current reading. Checks of one entity run one at a time (see
    :func:`outdoor_check_lock`). The verdict is stored in ``call_for_heat``.
    """
    async with outdoor_check_lock(self):
        return await _check_ambient_air_temperature(self)


async def _check_ambient_air_temperature(self: BetterThermostat) -> None:
    """Decide call_for_heat from the outdoor sensor; callers hold the lock."""
    outdoor_sensor_entity_id = self.outdoor_sensor_entity_id
    if outdoor_sensor_entity_id is None:
        return None

    if self.off_temperature is None or not isinstance(self.off_temperature, float):
        _LOGGER.warning(
            "better_thermostat %s: off_temperature not set or not a float.",
            self.device_name,
        )
        return None

    # Check if outdoor sensor is available
    outdoor_state = self.hass.states.get(outdoor_sensor_entity_id)
    if outdoor_state is None or outdoor_state.state in ("unavailable", "unknown", None):
        _LOGGER.debug(
            "better_thermostat %s: outdoor sensor %s unavailable, skipping ambient check",
            self.device_name,
            outdoor_sensor_entity_id,
        )
        # Keep last known value or default to heating enabled
        if self.damped_outdoor_temperature is None:
            self.call_for_heat = True
        return None

    unit = outdoor_state.attributes.get("unit_of_measurement")

    def reading_of(state: State) -> float | None:
        if state.state in ("unknown", "unavailable"):
            return None
        return convert_to_float_celsius(
            state.state,
            self.device_name,
            "check_ambient_air_temperature()",
            unit_of_measurement=state.attributes.get("unit_of_measurement") or unit,
        )

    damped_temperature = await _damp_live_reading(
        self,
        self.outdoor_source,
        outdoor_sensor_entity_id,
        outdoor_state,
        reading_of,
        attribute_changes=False,
        decides=True,
    )
    if damped_temperature is None:
        # Neither history nor the current state holds a usable reading.
        self.damped_outdoor_temperature = None
        self.call_for_heat = True
        return None

    threshold = heat_threshold(self.off_temperature, self.call_for_heat)
    _LOGGER.debug(
        "better_thermostat %s: damped outdoor temperature: %.2f, heating below %.2f",
        self.device_name,
        damped_temperature,
        threshold,
    )
    self.call_for_heat = damped_temperature < threshold
    self.damped_outdoor_temperature = damped_temperature


async def _damp_history(
    self: BetterThermostat,
    entity_id: str,
    reading_of: Callable[[State], float | None],
    off_temperature: float,
    *,
    attribute_changes: bool,
) -> tuple[DampedOutdoorTemperature | None, bool]:
    """Run an outdoor source's recorder history through the filter.

    The summer-mode decision follows the damped temperature along the
    history, so a restart finds the room on the side of the hysteresis band
    it was on before. Between two readings the damped temperature moves
    towards the held reading without turning back, so deciding at each
    reading catches every crossing.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the outdoor sensor or weather entity
    reading_of :
            turns a recorded state into °C, None for an unusable one
    off_temperature :
            the summer-mode threshold, in °C
    attribute_changes :
            also read states whose attributes changed but whose state did
            not; a weather entity's state is the condition, and its
            temperature is an attribute

    Returns
    -------
    tuple
            the filter state after the last recorded reading, None if the
            history holds no usable reading, and whether the room heats
            after it; a room starts the history heating
    """
    end = self.clock.utcnow()
    start = end - OUTDOOR_HISTORY_WINDOW
    lower_entity_id = entity_id.lower()
    recorder = get_instance(self.hass)
    if attribute_changes:
        history_list = await recorder.async_add_executor_job(
            partial(
                history.get_significant_states,
                self.hass,
                start,
                end,
                [lower_entity_id],
                significant_changes_only=False,
            )
        )
    else:
        history_list = await recorder.async_add_executor_job(
            history.state_changes_during_period, self.hass, start, end, lower_entity_id
        )
    items: list[State] = []
    try:
        items = [
            item
            for item in history_list.get(lower_entity_id) or []
            if not isinstance(item, dict)
        ]
    except AttributeError, KeyError, TypeError:
        items = []
    damping: DampedOutdoorTemperature | None = None
    call_for_heat = True
    for item in sorted(items, key=lambda item: item.last_updated):
        reading = reading_of(item)
        if reading is None:
            continue
        reading_at = item.last_updated.timestamp()
        damping = (
            start_damping(reading, reading_at)
            if damping is None
            else add_reading(damping, reading, reading_at)
        )
        call_for_heat = damping.value < heat_threshold(off_temperature, call_for_heat)
    _LOGGER.debug(
        "better_thermostat %s: damped %d recorded states of %s",
        self.device_name,
        len(items),
        entity_id,
    )
    return damping, call_for_heat


def _source_facts(prefix: str, source: DampedSource) -> dict[str, object]:
    """Return the diagnostics of one damped source under ``prefix``."""
    damping = source.damping
    return {
        f"{prefix}_reading": None if damping is None else damping.reading,
        f"{prefix}_reading_at": (
            None
            if damping is None
            else datetime.fromtimestamp(damping.reading_at, UTC).isoformat()
        ),
        f"{prefix}_history_damped": source.history_damped,
        f"{prefix}_history_failing": source.history_failing,
    }


def summer_mode_facts(self: BetterThermostat) -> dict[str, object]:
    """Return what the summer-mode decision rests on, for the diagnostics.

    Temperatures are °C. ``heat_threshold`` is the outdoor temperature
    below which the room heats, given its current decision; it is None
    without an ``off_temperature``.
    """
    return {
        "call_for_heat": self.call_for_heat,
        "off_temperature": self.off_temperature,
        "heat_threshold": (
            None
            if self.off_temperature is None
            else heat_threshold(self.off_temperature, self.call_for_heat)
        ),
        "damped_outdoor_temperature": self.damped_outdoor_temperature,
        **_source_facts("outdoor", self.outdoor_source),
        "damped_weather_temperature": self.damped_weather_temperature,
        "forecast_temperature": self.forecast_temperature,
        **_source_facts("weather", self.weather_source),
        "weather_fallback_active": self.weather_fallback_active,
    }
