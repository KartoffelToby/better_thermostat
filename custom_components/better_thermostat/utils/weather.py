"""Weather utils."""

from collections import deque
import logging
from datetime import timedelta, datetime
import homeassistant.util.dt as dt_util

# get_instance location can differ between HA versions; prefer helpers API.
from homeassistant.helpers.recorder import get_instance
from homeassistant.components.recorder import history
from contextlib import suppress

# from datetime import datetime, timedelta

# import homeassistant.util.dt as dt_util
# from homeassistant.components.recorder.history import state_changes_during_period

from .helpers import convert_to_float

from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
    WeatherEntityFeature,
)
from homeassistant.exceptions import ServiceNotSupported, HomeAssistantError

_LOGGER = logging.getLogger(__name__)


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

    self.call_for_heat = True

    if self.weather_entity is not None:
        _call_for_heat_weather = await check_weather_prediction(self)
        if isinstance(
            _call_for_heat_weather, bool
        ):  # Only apply if we got a valid response
            self.call_for_heat = _call_for_heat_weather

    if self.outdoor_sensor is not None:
        if None in (self.last_avg_outdoor_temp, self.off_temperature):
            # TODO: add condition if heating period (oct-mar) then set it to true?
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
        return True

    if old_call_for_heat != self.call_for_heat:
        return True
    else:
        return False


async def check_weather_prediction(self) -> bool | None:
    """Check configured weather entity for next two days of temperature predictions.

    Returns
    -------
    bool
            True if the maximum forcast temperature is lower than the off temperature
    None
            if not successful
    """
    if self.weather_entity is None:
        _LOGGER.warning(
            "better_thermostat %s: weather entity not available.", self.device_name
        )
        return False

    if self.off_temperature is None or not isinstance(self.off_temperature, float):
        _LOGGER.warning(
            "better_thermostat %s: off_temperature not set or not a float.",
            self.device_name,
        )
        return False

    try:
        state = self.hass.states.get(self.weather_entity)
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
                self.weather_entity,
            )
            return None

        forecasts = await self.hass.services.async_call(
            WEATHER_DOMAIN,
            "get_forecasts",
            {"type": ftype, "entity_id": [self.weather_entity]},
            blocking=True,
            return_response=True,
        )
        forecast_container = (
            forecasts.get(self.weather_entity) if isinstance(forecasts, dict) else None
        )
        forecast = (
            forecast_container.get("forecast")
            if isinstance(forecast_container, dict)
            else None
        )
        if isinstance(forecast, list) and len(forecast) > 0:
            # current outside temp from entity state (may be None)
            cur_state = self.hass.states.get(self.weather_entity)
            cur_outside_temp = convert_to_float(
                (
                    str(cur_state.attributes.get("temperature"))
                    if cur_state and cur_state.attributes
                    else ""
                ),
                self.device_name,
                "check_weather_prediction()",
            )
            # compute simple average of first up-to-2 daily temps
            temps = []
            for i in range(min(2, len(forecast))):
                temps.append(
                    convert_to_float(
                        (
                            str(forecast[i].get("temperature"))
                            if isinstance(forecast[i], dict)
                            else ""
                        ),
                        self.device_name,
                        "check_weather_prediction()",
                    )
                )
            temps = [t for t in temps if isinstance(t, (int, float))]
            max_forecast_temp = None
            if temps:
                max_forecast_temp = sum(temps) / float(len(temps))

            cond_cur = (
                isinstance(cur_outside_temp, (int, float))
                and cur_outside_temp < self.off_temperature
            )
            cond_fc = (
                isinstance(max_forecast_temp, (int, float))
                and max_forecast_temp < self.off_temperature
            )
            return bool(cond_cur or cond_fc)
        else:
            raise TypeError
    except (TypeError, ServiceNotSupported, HomeAssistantError):
        _LOGGER.warning(
            "better_thermostat %s: no weather entity data found.", self.device_name
        )
        return False


async def check_ambient_air_temperature(self):
    """Get the history for two days and evaluates the necessary for heating.

    Returns
    -------
    bool
            True if the average temperature is lower than the off temperature
    None
            if not successful
    """
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

    self.last_avg_outdoor_temp = convert_to_float(
        outdoor_state.state, self.device_name, "check_ambient_air_temperature()"
    )
    if "recorder" in self.hass.config.components:
        _temp_history = DailyHistory(2)
        start_date = dt_util.utcnow() - timedelta(days=2)
        entity_id = self.outdoor_sensor
        if entity_id is None:
            _LOGGER.debug(
                "Not reading the history from the database as "
                "there is no outdoor sensor configured"
            )
            return
        _LOGGER.debug(
            "Initializing values for %s from the database", self.outdoor_sensor
        )
        lower_entity_id = entity_id.lower()
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
        except (AttributeError, KeyError, TypeError):
            items = []
        for item in items:
            # filter out all None, NaN, "unknown" and "unavailable" states.
            # only keep real values
            with suppress(ValueError):
                if item.state not in ("unknown", "unavailable"):
                    _temp_history.add_measurement(
                        convert_to_float(
                            item.state,
                            self.device_name,
                            "check_ambient_air_temperature()",
                        ),
                        datetime.fromtimestamp(item.last_updated.timestamp()),
                    )

        avg_temp = _temp_history.min

        _LOGGER.debug("Initializing from database completed")
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


class DailyHistory:
    """Stores one measurement per day for a maximum number of days.
    We compute an average outside temperature that better reflects the last days:
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
        day = (timestamp or datetime.now()).date()
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
