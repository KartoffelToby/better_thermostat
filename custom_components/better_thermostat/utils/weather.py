from collections import deque
import logging
from datetime import timedelta, datetime
import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance, history
from contextlib import suppress

# from datetime import datetime, timedelta

# import homeassistant.util.dt as dt_util
# from homeassistant.components.recorder.history import state_changes_during_period

from .helpers import convert_to_float
from statistics import median


_LOGGER = logging.getLogger(__name__)


def check_weather(self) -> bool:
    """check weather predictions or ambient air temperature if available

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
    _call_for_heat_weather = False
    _call_for_heat_outdoor = False

    self.call_for_heat = True

    if self.weather_entity is not None:
        _call_for_heat_weather = check_weather_prediction(self)
        self.call_for_heat = _call_for_heat_weather

    if self.outdoor_sensor is not None:
        if None in (self.last_avg_outdoor_temp, self.off_temperature):
            # TODO: add condition if heating period (oct-mar) then set it to true?
            _LOGGER.warning(
                "better_thermostat %s: no outdoor sensor data found. fallback to heat",
                self.name,
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


def check_weather_prediction(self) -> bool:
    """Checks configured weather entity for next two days of temperature predictions.

    Returns
    -------
    bool
            True if the maximum forcast temperature is lower than the off temperature
    None
            if not successful
    """
    if self.weather_entity is None:
        _LOGGER.warning(f"better_thermostat {self.name}: weather entity not available.")
        return None

    if self.off_temperature is None or not isinstance(self.off_temperature, float):
        _LOGGER.warning(
            f"better_thermostat {self.name}: off_temperature not set or not a float."
        )
        return None

    try:
        forecast = self.hass.states.get(self.weather_entity).attributes.get("forecast")
        if len(forecast) > 0:
            cur_outside_temp = convert_to_float(
                str(
                    self.hass.states.get(self.weather_entity).attributes.get(
                        "temperature"
                    )
                ),
                self.name,
                "check_weather_prediction()",
            )
            max_forecast_temp = int(
                round(
                    (
                        convert_to_float(
                            str(forecast[0]["temperature"]),
                            self.name,
                            "check_weather_prediction()",
                        )
                        + convert_to_float(
                            str(forecast[1]["temperature"]),
                            self.name,
                            "check_weather_prediction()",
                        )
                    )
                    / 2
                )
            )
            return (
                cur_outside_temp < self.off_temperature
                or max_forecast_temp < self.off_temperature
            )
        else:
            raise TypeError
    except TypeError:
        _LOGGER.warning(f"better_thermostat {self.name}: no weather entity data found.")
        return None


async def check_ambient_air_temperature(self):
    """Gets the history for two days and evaluates the necessary for heating.

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
            f"better_thermostat {self.name}: off_temperature not set or not a float."
        )
        return None

    self.last_avg_outdoor_temp = convert_to_float(
        self.hass.states.get(self.outdoor_sensor).state,
        self.name,
        "check_ambient_air_temperature()",
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

        for item in history_list.get(lower_entity_id):
            # filter out all None, NaN and "unknown" states
            # only keep real values
            with suppress(ValueError):
                if item.state != "unknown":
                    _temp_history.add_measurement(
                        convert_to_float(
                            item.state, self.name, "check_ambient_air_temperature()"
                        ),
                        datetime.fromtimestamp(item.last_updated.timestamp()),
                    )

        avg_temp = _temp_history.min

        _LOGGER.debug("Initializing from database completed")
    else:
        avg_temp = self.last_avg_outdoor_temp

    _LOGGER.debug(
        f"better_thermostat {self.name}: avg outdoor temp: {avg_temp}, threshold is {self.off_temperature}"
    )

    if avg_temp is not None:
        self.call_for_heat = avg_temp < self.off_temperature
    else:
        self.call_for_heat = True

    self.last_avg_outdoor_temp = avg_temp


class DailyHistory:
    """Stores one measurement per day for a maximum number of days.
    At the moment only the maximum value per day is kept.
    """

    def __init__(self, max_length):
        """Create new DailyHistory with a maximum length of the history."""
        self.max_length = max_length
        self._days = None
        self._max_dict = {}
        self.min = None

    def add_measurement(self, value, timestamp=None):
        """Add a new measurement for a certain day."""
        day = (timestamp or datetime.now()).date()
        if not isinstance(value, (int, float)):
            return
        if self._days is None:
            self._days = deque()
            self._add_day(day, value)
        else:
            current_day = self._days[-1]
            if day == current_day:
                self._max_dict[day] = min(value, self._max_dict[day])
            elif day > current_day:
                self._add_day(day, value)
            else:
                _LOGGER.warning("Received old measurement, not storing it")

        self.min = median(self._max_dict.values())

    def _add_day(self, day, value):
        """Add a new day to the history.
        Deletes the oldest day, if the queue becomes too long.
        """
        if len(self._days) == self.max_length:
            oldest = self._days.popleft()
            del self._max_dict[oldest]
        self._days.append(day)
        if not isinstance(value, (int, float)):
            return
        self._max_dict[day] = value
