import logging
from datetime import datetime, timedelta
import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance, history

# from datetime import datetime, timedelta

# import homeassistant.util.dt as dt_util
# from homeassistant.components.recorder.history import state_changes_during_period

from .helpers import convert_to_float

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

    if self.weather_entity is not None:
        self.call_for_heat = check_weather_prediction(self)

    elif self.outdoor_sensor is not None:
        if None in (self.last_avg_outdoor_temp, self.off_temperature):
            self.call_for_heat = False
            return False
        self.call_for_heat = self.last_avg_outdoor_temp < self.off_temperature
    else:
        self.call_for_heat = True

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
        forcast = self.hass.states.get(self.weather_entity).attributes.get("forecast")
        if len(forcast) > 0:
            max_forcast_temp = int(
                round(
                    (
                        convert_to_float(
                            str(forcast[0]["temperature"]),
                            self.name,
                            "check_weather_prediction()",
                        )
                        + convert_to_float(
                            str(forcast[1]["temperature"]),
                            self.name,
                            "check_weather_prediction()",
                        )
                    )
                    / 2
                )
            )
            return max_forcast_temp < self.off_temperature
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

    last_two_days_date_time = datetime.now() - timedelta(days=2)
    start = dt_util.as_utc(last_two_days_date_time)
    history_list = await get_instance(self.hass).async_add_executor_job(
        history.state_changes_during_period,
        self.hass,
        start,
        dt_util.as_utc(datetime.now()),
        str(self.outdoor_sensor),
    )
    historic_sensor_data = history_list.get(self.outdoor_sensor)
    # create a list from valid data in historic_sensor_data
    valid_historic_sensor_data = []
    invalid_sensor_data_count = 0
    if historic_sensor_data is not None:
        _LOGGER.warning(
            f"better_thermostat {self.name}: {self.outdoor_sensor} has no historic data."
        )
        return convert_to_float(
            self.hass.states.get(self.outdoor_sensor).state,
            self.name,
            "check_ambient_air_temperature()",
        )
    for measurement in historic_sensor_data:
        if isinstance(
            measurement := convert_to_float(
                str(measurement.state), self.name, "check_ambient_air_temperature()"
            ),
            float,
        ):
            valid_historic_sensor_data.append(measurement)
        else:
            invalid_sensor_data_count += 1

    if len(valid_historic_sensor_data) == 0:
        _LOGGER.warning(
            f"better_thermostat {self.name}: no valid outdoor sensor data found."
        )
        return None

    if invalid_sensor_data_count:
        _LOGGER.debug(
            f"better_thermostat {self.name}: ignored {invalid_sensor_data_count} invalid outdoor sensor data entries."
        )

    # remove the upper and lower 5% of the data
    valid_historic_sensor_data.sort()
    valid_historic_sensor_data = valid_historic_sensor_data[
        int(round(len(valid_historic_sensor_data) * 0.05)) : int(
            round(len(valid_historic_sensor_data) * 0.95)
        )
    ]

    if len(valid_historic_sensor_data) == 0:
        _LOGGER.warning(
            f"better_thermostat {self.name}: no valid outdoor sensor data found."
        )
        return None

    _LOGGER.debug(
        f"better_thermostat {self.name}: check_ambient_air_temperature is evaluating {len(valid_historic_sensor_data)} sensor values."
    )

    # calculate the average temperature
    avg_temp = int(
        round(sum(valid_historic_sensor_data) / len(valid_historic_sensor_data))
    )
    _LOGGER.debug(
        f"better_thermostat {self.name}: avg outdoor temp: {avg_temp}, threshold is {self.off_temperature}"
    )

    self.last_avg_outdoor_temp = avg_temp
