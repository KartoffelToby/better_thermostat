"""Weather utils."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import date, datetime, timedelta
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
import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

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

# How long check_ambient_air_temperature reuses the two-day outdoor mean it
# read from the recorder. The check runs on every outdoor sensor update, and a
# quarter of an hour of new readings barely moves a mean of per-day means, so
# the verdict lags a fresh read by at most this interval.
OUTDOOR_HISTORY_REFRESH = timedelta(minutes=15)

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
            _silent_seconds = _now - self.weather_verdict_missing_since
            if (
                not self.weather_fallback_active
                and _silent_seconds >= WEATHER_VERDICT_HOLD.total_seconds()
            ):
                _LOGGER.warning(
                    "better_thermostat %s: weather entity %s has given no forecast "
                    "for %.1f hours, resuming heating until it does",
                    self.device_name,
                    self.weather_entity_id,
                    _silent_seconds / 3600.0,
                )
                self.weather_fallback_active = True
            if self.weather_fallback_active:
                self.call_for_heat = True

    if self.outdoor_sensor_entity_id is not None:
        if self.last_avg_outdoor_temperature is None or self.off_temperature is None:
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
                self.last_avg_outdoor_temperature < self.off_temperature
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
            temperature) is below the off temperature, i.e. heat is required
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

        if features & WeatherEntityFeature.FORECAST_DAILY:
            ftype = "daily"
        elif features & WeatherEntityFeature.FORECAST_TWICE_DAILY:
            ftype = "twice_daily"
        elif features & WeatherEntityFeature.FORECAST_HOURLY:
            ftype = "hourly"
        else:
            _LOGGER.warning(
                "better_thermostat %s: weather entity '%s' does not advertise any forecast support.",
                self.device_name,
                self.weather_entity_id,
            )
            return None

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
            # current outside temperature from entity state (may be None)
            cur_state = self.hass.states.get(self.weather_entity_id)
            current_outdoor_temperature = convert_to_float_celsius(
                (
                    str(cur_state.attributes.get("temperature"))
                    if cur_state and cur_state.attributes
                    else ""
                ),
                self.device_name,
                "check_weather_prediction()",
                unit_of_measurement=(
                    cur_state.attributes.get("temperature_unit")
                    if cur_state and cur_state.attributes
                    else None
                ),
            )
            # average the sampled forecast temps over the two-day horizon
            _entity_temperature_unit = (
                cur_state.attributes.get("temperature_unit")
                if cur_state and cur_state.attributes
                else None
            )
            temps: list[float | None] = []
            for entry in forecast[:_forecast_samples]:
                _entry_unit = (
                    entry.get("temperature_unit") if isinstance(entry, dict) else None
                )
                temps.append(
                    convert_to_float_celsius(
                        (
                            str(entry.get("temperature"))
                            if isinstance(entry, dict)
                            else ""
                        ),
                        self.device_name,
                        "check_weather_prediction()",
                        unit_of_measurement=(
                            _entry_unit
                            if isinstance(_entry_unit, str)
                            else _entity_temperature_unit
                        ),
                    )
                )
            valid_temps: list[float] = [t for t in temps if isinstance(t, (int, float))]
            avg_forecast_temperature = None
            if valid_temps:
                avg_forecast_temperature = sum(valid_temps) / float(len(valid_temps))

            # A forecast whose entries and current reading are all unusable
            # carries no temperature at all, so it gives no opinion rather
            # than the "warm" an empty comparison would read as.
            if avg_forecast_temperature is None and not isinstance(
                current_outdoor_temperature, (int, float)
            ):
                return None
            cond_cur = (
                isinstance(current_outdoor_temperature, (int, float))
                and current_outdoor_temperature < self.off_temperature
            )
            cond_fc = (
                isinstance(avg_forecast_temperature, (int, float))
                and avg_forecast_temperature < self.off_temperature
            )
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


def outdoor_check_lock(self: BetterThermostat) -> asyncio.Lock:
    """Return the lock that serialises this entity's ambient air check.

    The check stores the live outdoor reading on the entity, may suspend
    while the recorder is read, and then decides on the mean or, without
    usable history, on that stored reading. The periodic tick and the outdoor
    sensor listener run the check in their own tasks. Without the lock a
    second check that completes during the first one's recorder read
    overwrites the stored reading with the cached mean, and the first check
    then falls back to that mean instead of its live reading. The lock is
    created on first use and lives on the entity, so each Better Thermostat
    only queues behind itself.

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


async def check_ambient_air_temperature(self: BetterThermostat) -> None:
    """Get the history for two days and evaluates the necessary for heating.

    The two-day mean from the recorder is reused for
    ``OUTDOOR_HISTORY_REFRESH`` before it is read again. When the recorder
    holds no usable history, the current reading decides instead. Checks of
    one entity run one at a time (see :func:`outdoor_check_lock`). The
    verdict is stored in ``call_for_heat``.
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
        if self.last_avg_outdoor_temperature is None:
            self.call_for_heat = True
        return None

    self.last_avg_outdoor_temperature = convert_to_float_celsius(
        outdoor_state.state,
        self.device_name,
        "check_ambient_air_temperature()",
        unit_of_measurement=outdoor_state.attributes.get("unit_of_measurement"),
    )
    if "recorder" in self.hass.config.components:
        _now = self.clock.monotonic()
        if (
            self.outdoor_history_read_at is None
            or _now - self.outdoor_history_read_at
            >= OUTDOOR_HISTORY_REFRESH.total_seconds()
        ):
            self.outdoor_history_read_at = _now
            try:
                self.outdoor_history_mean = await _read_outdoor_history_mean(
                    self, outdoor_sensor_entity_id, outdoor_state
                )
            except SQLAlchemyError, RuntimeError, HomeAssistantError, OSError:
                # The recorder logs the traceback itself. Warn once per run of
                # failures; repeats go to the debug log.
                _LOGGER.log(
                    logging.DEBUG if self.outdoor_history_failing else logging.WARNING,
                    "better_thermostat %s: reading the history of %s from the "
                    "recorder failed, keeping the last known outdoor mean",
                    self.device_name,
                    outdoor_sensor_entity_id,
                )
                self.outdoor_history_failing = True
            else:
                self.outdoor_history_failing = False

        avg_temperature = self.outdoor_history_mean
        if avg_temperature is None:
            # No usable recorder history (e.g. a freshly created helper or a
            # sensor the recorder does not retain). Fall back to the current
            # reading so the outdoor threshold still applies instead of
            # defaulting to "heat".
            avg_temperature = self.last_avg_outdoor_temperature
    else:
        avg_temperature = self.last_avg_outdoor_temperature

    _LOGGER.debug(
        "better_thermostat %s: avg outdoor temp: %s, threshold is %s",
        self.device_name,
        avg_temperature,
        self.off_temperature,
    )

    if avg_temperature is not None:
        self.call_for_heat = avg_temperature < self.off_temperature
    else:
        self.call_for_heat = True

    self.last_avg_outdoor_temperature = avg_temperature


async def _read_outdoor_history_mean(
    self: BetterThermostat, entity_id: str, outdoor_state: State
) -> float | None:
    """Return the two-day mean of the outdoor sensor's recorder history.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            entity id of the outdoor sensor
    outdoor_state :
            current state of the outdoor sensor, whose unit applies to history
            items that carry none

    Returns
    -------
    float or None
            mean of the per-day means, None if the history holds no usable
            reading
    """
    _temperature_history = DailyHistory(2)
    start_date = dt_util.utcnow() - timedelta(days=2)
    _LOGGER.debug("Initializing values for %s from the database", entity_id)
    lower_entity_id = entity_id.lower()
    history_list = await get_instance(self.hass).async_add_executor_job(
        history.state_changes_during_period,
        self.hass,
        start_date,
        dt_util.utcnow(),
        lower_entity_id,
    )
    items: list[State] = []
    try:
        items = history_list.get(lower_entity_id) or []
    except AttributeError, KeyError, TypeError:
        items = []
    for item in items:
        # filter out all None, NaN, "unknown" and "unavailable" states.
        # only keep real values
        with suppress(ValueError):
            if item.state not in ("unknown", "unavailable"):
                _temperature_history.add_measurement(
                    convert_to_float_celsius(
                        item.state,
                        self.device_name,
                        "check_ambient_air_temperature()",
                        unit_of_measurement=(
                            item.attributes.get("unit_of_measurement")
                            or outdoor_state.attributes.get("unit_of_measurement")
                        ),
                    ),
                    datetime.fromtimestamp(item.last_updated.timestamp()),
                )
    _LOGGER.debug("Initializing from database completed")
    return _temperature_history.min


class DailyHistory:
    """Store one measurement per day for a maximum number of days.

    Compute an average outside temperature that better reflects the last days:
      - Track all readings per day and compute the per-day mean
      - Then compute the overall mean across the kept days

    Note: the attribute holding the result is named `min` and carries the
    multi-day mean as a float.
    """

    def __init__(self, max_length: int) -> None:
        """Create new DailyHistory with a maximum length of the history."""
        self.max_length = max_length
        self._days: deque[date] | None = None
        # Track per-day aggregate to compute means
        self._sum_dict: dict[date, float] = {}
        self._count_dict: dict[date, int] = {}
        # Holds the resulting multi-day mean
        self.min: float | None = None

    def add_measurement(
        self, value: float | None, timestamp: datetime | None = None
    ) -> None:
        """Add a new measurement for a certain day (value: float)."""
        day = (timestamp or dt_util.now()).date()
        if not isinstance(value, (int, float)):
            return
        if self._days is None:
            self._days = deque()
            self._add_day(day, value)
        else:
            current_day = self._days[-1]
            if day == current_day:
                # Accumulate for the same day
                self._sum_dict[day] = self._sum_dict.get(day, 0.0) + float(value)
                self._count_dict[day] = self._count_dict.get(day, 0) + 1
            elif day > current_day:
                self._add_day(day, value)
            else:
                _LOGGER.debug(
                    "DailyHistory: received out-of-order measurement, skipping"
                )

        # Compute per-day means and then the overall mean across days
        day_means: list[float] = []
        if self._days:
            for d in self._days:
                cnt = self._count_dict.get(d, 0)
                if cnt > 0:
                    day_means.append(self._sum_dict.get(d, 0.0) / float(cnt))
        if day_means:
            self.min = sum(day_means) / float(len(day_means))

    def _add_day(self, day: date, value: float) -> None:
        """Add a new day to the history.

        Deletes the oldest day, if the queue becomes too long.
        """
        if self._days is None:
            self._days = deque()
        if len(self._days) == self.max_length:
            oldest = self._days.popleft()
            # Clean up aggregates of the removed day
            self._sum_dict.pop(oldest, None)
            self._count_dict.pop(oldest, None)
        self._days.append(day)
        # Initialize aggregates for the new day with the first value
        self._sum_dict[day] = float(value)
        self._count_dict[day] = 1
