"""Weather utils."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from datetime import datetime, timedelta
import logging
from time import monotonic

from homeassistant.components.recorder import history
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    WeatherEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError, ServiceNotSupported

# get_instance location can differ between HA versions; prefer helpers API.
from homeassistant.helpers.recorder import get_instance
import homeassistant.util.dt as dt_util
from sqlalchemy.exc import SQLAlchemyError

# from datetime import datetime, timedelta
# import homeassistant.util.dt as dt_util
# from homeassistant.components.recorder.history import state_changes_during_period
from .helpers import convert_to_float_celsius
from .weather_cache import remember_weather_forecast, usable_weather_cache

_LOGGER = logging.getLogger(__name__)

# How long the get_forecasts service call may take. Coordinator-based weather
# entities answer from memory; one that fetches on demand answers within a
# few seconds while its service is reachable. A cloud integration can instead
# stall until its HTTP client gives up, minutes later, when the internet is
# down, and startup awaits this call before the entity becomes available. A
# call cut off here reads as a missing forecast.
FORECAST_CALL_TIMEOUT = timedelta(seconds=10)

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


async def check_weather(self) -> bool:
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
    _heats_without_verdict = False

    if self.weather_entity is not None:
        _call_for_heat_weather = await check_weather_prediction(self)
        if isinstance(_call_for_heat_weather, bool):
            if self.weather_fallback_active:
                _LOGGER.info(
                    "better_thermostat %s: weather entity %s gives a forecast "
                    "verdict again, heating follows the forecast",
                    self.device_name,
                    self.weather_entity,
                )
            self.weather_verdict_missing_since = None
            self.weather_fallback_active = False
            self.weather_verdict_received = True
            self.call_for_heat = _call_for_heat_weather
        elif self.outdoor_sensor is None:
            # None means the prediction has no opinion: the previous decision
            # stays for WEATHER_VERDICT_HOLD, then the room heats. With an
            # outdoor sensor configured its verdict decides below, so the
            # hold only applies where the forecast is the only source, and
            # only to a verdict the forecast gave since startup: a decision
            # restored from saved state is not held, the room heats.
            # Monotonic time keeps the hold its length across a DST change.
            _now = monotonic()
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
                    self.weather_entity,
                    _silent_s / 3600.0,
                )
                self.weather_fallback_active = True
            if self.weather_fallback_active or not self.weather_verdict_received:
                self.call_for_heat = True
                _heats_without_verdict = True

    if self.outdoor_sensor is not None:
        if None in (self.last_avg_outdoor_temp, self.off_temperature):
            # Check if sensor is currently unavailable (expected during startup)
            _outdoor_state = self.hass.states.get(self.outdoor_sensor)
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
            _call_for_heat_outdoor = self.last_avg_outdoor_temp < self.off_temperature

        self.call_for_heat = _call_for_heat_outdoor

    if self.weather_entity is None and self.outdoor_sensor is None:
        self.call_for_heat = True

    if old_call_for_heat != self.call_for_heat:
        from custom_components.better_thermostat.utils.helpers import (
            async_fire_logbook_entry,
        )

        if not self.call_for_heat:
            await async_fire_logbook_entry(
                self,
                "summer_mode_on",
                "turned off because the outdoor temperature is too high",
            )
        elif _heats_without_verdict:
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


def _cached_weather_verdict(self) -> bool | None:
    """Reuse remaining forecast slots; expired/misconfigured caches give no verdict."""
    slots = usable_weather_cache(self)
    if not slots:
        return None
    avg = sum(item["temperature"] for item in slots) / len(slots)
    return avg < self.off_temperature


async def check_weather_prediction(self) -> bool | None:
    """Decide the heating season from a live forecast or a validated cached one.

    Prefer hourly forecasts for an exact wall-clock fallback on connectivity loss.
    Missing or expired predictions never silently masquerade as live data.
    """
    if self.weather_entity is None:
        _LOGGER.warning(
            "better_thermostat %s: weather entity not configured.", self.device_name
        )
        return False
    if self.off_temperature is None or not isinstance(self.off_temperature, float):
        _LOGGER.warning(
            "better_thermostat %s: off_temperature not set or not a float.",
            self.device_name,
        )
        return None

    state = self.hass.states.get(self.weather_entity)
    if state is None or state.state in ("unavailable", "unknown", None):
        return _cached_weather_verdict(self)

    features = state.attributes.get("supported_features", 0)
    if features & WeatherEntityFeature.FORECAST_HOURLY:
        ftype = "hourly"
    elif features & WeatherEntityFeature.FORECAST_TWICE_DAILY:
        ftype = "twice_daily"
    elif features & WeatherEntityFeature.FORECAST_DAILY:
        ftype = "daily"
    else:
        _LOGGER.warning(
            "better_thermostat %s: weather entity '%s' has no forecast support.",
            self.device_name,
            self.weather_entity,
        )
        return _cached_weather_verdict(self)

    samples = {"daily": 2, "twice_daily": 4, "hourly": 48}[ftype]
    try:
        async with asyncio.timeout(FORECAST_CALL_TIMEOUT.total_seconds()):
            response = await self.hass.services.async_call(
                WEATHER_DOMAIN,
                "get_forecasts",
                {"type": ftype, "entity_id": [self.weather_entity]},
                blocking=True,
                return_response=True,
            )
    except TimeoutError:
        _LOGGER.warning(
            "better_thermostat %s: forecast %s timed out after %.0fs",
            self.device_name,
            self.weather_entity,
            FORECAST_CALL_TIMEOUT.total_seconds(),
        )
        return _cached_weather_verdict(self)
    except TypeError, ServiceNotSupported, HomeAssistantError:
        _LOGGER.warning(
            "better_thermostat %s: no weather forecast data found.", self.device_name
        )
        return _cached_weather_verdict(self)

    container = (
        response.get(self.weather_entity) if isinstance(response, dict) else None
    )
    forecast = container.get("forecast") if isinstance(container, dict) else None
    if not isinstance(forecast, list) or not forecast:
        return _cached_weather_verdict(self)

    unit = state.attributes.get("temperature_unit")
    current_temp = convert_to_float_celsius(
        str(state.attributes.get("temperature")),
        self.device_name,
        "check_weather_prediction()",
        unit_of_measurement=unit,
    )
    temps = [
        convert_to_float_celsius(
            str(row.get("temperature", "")) if isinstance(row, dict) else "",
            self.device_name,
            "check_weather_prediction()",
            unit_of_measurement=row.get("temperature_unit", unit)
            if isinstance(row, dict)
            else unit,
        )
        for row in forecast[:samples]
    ]
    valid = [t for t in temps if isinstance(t, (int, float))]
    if not valid and not isinstance(current_temp, (int, float)):
        return _cached_weather_verdict(self)

    # Only timestamped, finite temperatures are persisted. A provider that
    # omits datetime gives a live verdict but no offline forecast.
    remember_weather_forecast(self, ftype, forecast[:samples], current_unit=unit)
    avg_forecast = sum(valid) / len(valid) if valid else None
    return bool(
        (isinstance(current_temp, (int, float)) and current_temp < self.off_temperature)
        or (
            isinstance(avg_forecast, (int, float))
            and avg_forecast < self.off_temperature
        )
    )


def outdoor_check_lock(self) -> asyncio.Lock:
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
    lock = getattr(self, "_outdoor_check_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        self._outdoor_check_lock = lock
    return lock


async def check_ambient_air_temperature(self):
    """Get the history for two days and evaluates the necessary for heating.

    The two-day mean from the recorder is reused for
    ``OUTDOOR_HISTORY_REFRESH`` before it is read again. When the recorder
    holds no usable history, the current reading decides instead. Checks of
    one entity run one at a time (see :func:`outdoor_check_lock`).

    Returns
    -------
    bool
            True if the average temperature is lower than the off temperature
    None
            if not successful
    """
    async with outdoor_check_lock(self):
        return await _check_ambient_air_temperature(self)


async def _check_ambient_air_temperature(self):
    """Decide call_for_heat from the outdoor sensor; callers hold the lock."""
    if self.outdoor_sensor is None:
        return None

    if self.off_temperature is None or not isinstance(self.off_temperature, float):
        _LOGGER.warning(
            "better_thermostat %s: off_temperature not set or not a float.",
            self.device_name,
        )
        return None

    # Check if outdoor sensor is available
    outdoor_state = self.hass.states.get(self.outdoor_sensor)
    if outdoor_state is None or outdoor_state.state in ("unavailable", "unknown", None):
        _LOGGER.debug(
            "better_thermostat %s: outdoor sensor %s unavailable, skipping ambient check",
            self.device_name,
            self.outdoor_sensor,
        )
        # Keep last known value or default to heating enabled
        if self.last_avg_outdoor_temp is None:
            self.call_for_heat = True
        return None

    self.last_avg_outdoor_temp = convert_to_float_celsius(
        outdoor_state.state,
        self.device_name,
        "check_ambient_air_temperature()",
        unit_of_measurement=outdoor_state.attributes.get("unit_of_measurement"),
    )
    if "recorder" in self.hass.config.components:
        _now = monotonic()
        if (
            self.outdoor_history_read_at is None
            or _now - self.outdoor_history_read_at
            >= OUTDOOR_HISTORY_REFRESH.total_seconds()
        ):
            self.outdoor_history_read_at = _now
            try:
                self.outdoor_history_mean = await _read_outdoor_history_mean(
                    self, outdoor_state
                )
            except SQLAlchemyError, RuntimeError, HomeAssistantError, OSError:
                # The recorder logs the traceback itself. Warn once per run of
                # failures; repeats go to the debug log.
                _LOGGER.log(
                    logging.DEBUG if self.outdoor_history_failing else logging.WARNING,
                    "better_thermostat %s: reading the history of %s from the "
                    "recorder failed, keeping the last known outdoor mean",
                    self.device_name,
                    self.outdoor_sensor,
                )
                self.outdoor_history_failing = True
            else:
                self.outdoor_history_failing = False

        avg_temp = self.outdoor_history_mean
        if avg_temp is None:
            # No usable recorder history (e.g. a freshly created helper or a
            # sensor the recorder does not retain). Fall back to the current
            # reading so the outdoor threshold still applies instead of
            # defaulting to "heat".
            avg_temp = self.last_avg_outdoor_temp
    else:
        avg_temp = self.last_avg_outdoor_temp

    _LOGGER.debug(
        "better_thermostat %s: avg outdoor temp: %s, threshold is %s",
        self.device_name,
        avg_temp,
        self.off_temperature,
    )

    if avg_temp is not None:
        self.call_for_heat = avg_temp < self.off_temperature
    else:
        self.call_for_heat = True

    self.last_avg_outdoor_temp = avg_temp


async def _read_outdoor_history_mean(self, outdoor_state) -> float | None:
    """Return the two-day mean of the outdoor sensor's recorder history.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    outdoor_state :
            current state of the outdoor sensor, whose unit applies to history
            items that carry none

    Returns
    -------
    float or None
            mean of the per-day means, None if the history holds no usable
            reading
    """
    _temp_history = DailyHistory(2)
    start_date = dt_util.utcnow() - timedelta(days=2)
    _LOGGER.debug("Initializing values for %s from the database", self.outdoor_sensor)
    lower_entity_id = self.outdoor_sensor.lower()
    history_list = await get_instance(self.hass).async_add_executor_job(
        history.state_changes_during_period,
        self.hass,
        start_date,
        dt_util.utcnow(),
        lower_entity_id,
    )
    items = []
    try:
        items = history_list.get(lower_entity_id) or []
    except AttributeError, KeyError, TypeError:
        items = []
    for item in items:
        # filter out all None, NaN, "unknown" and "unavailable" states.
        # only keep real values
        with suppress(ValueError):
            if item.state not in ("unknown", "unavailable"):
                _temp_history.add_measurement(
                    convert_to_float_celsius(
                        item.state,
                        self.device_name,
                        "check_ambient_air_temperature()",
                        unit_of_measurement=(
                            getattr(item, "attributes", {}).get("unit_of_measurement")
                            or outdoor_state.attributes.get("unit_of_measurement")
                        ),
                    ),
                    datetime.fromtimestamp(item.last_updated.timestamp()),
                )
    _LOGGER.debug("Initializing from database completed")
    return _temp_history.min


class DailyHistory:
    """Store one measurement per day for a maximum number of days.

    Compute an average outside temperature that better reflects the last days:
      - Track all readings per day and compute the per-day mean
      - Then compute the overall mean across the kept days

    Note: Attribute name `min` is kept for backward compatibility with callers,
    but it now contains the multi-day mean (float) instead of a median of minima.
    """

    def __init__(self, max_length):
        """Create new DailyHistory with a maximum length of the history."""
        self.max_length = max_length
        self._days = None  # deque[date]
        # Track per-day aggregate to compute means
        self._sum_dict = {}
        self._count_dict = {}
        # Back-compat field: will store the resulting multi-day mean
        self.min = None

    def add_measurement(self, value, timestamp=None):
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
        day_means = []
        if self._days:
            for d in self._days:
                cnt = self._count_dict.get(d, 0)
                if cnt > 0:
                    day_means.append(self._sum_dict.get(d, 0.0) / float(cnt))
        if day_means:
            self.min = sum(day_means) / float(len(day_means))

    def _add_day(self, day, value):
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
        if not isinstance(value, (int, float)):
            return
        # Initialize aggregates for the new day with the first value
        self._sum_dict[day] = float(value)
        self._count_dict[day] = 1
