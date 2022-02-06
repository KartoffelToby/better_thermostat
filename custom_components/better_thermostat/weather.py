import logging
import math
import homeassistant.util.dt as dt_util

from homeassistant.components.recorder import history
from homeassistant.components.climate.const import (HVAC_MODE_OFF)
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

async def check_weather(self):
	# check weather predictions or ambient air temperature if available
	if self.weather_entity is not None:
		return check_weather_prediction(self)
	elif self.outdoor_sensor is not None:
		return check_ambient_air_temperature(self)
	else:
		_LOGGER.warning("better_thermostat: call for heat decision: could not evaluate sensor/weather entity data, force heat on")
		return True

def check_weather_prediction(self):
	"""
	Checks configured weather entity for next two days of temperature predictions.
	@return: True if the maximum forcast temperature is lower than the off temperature; None if not successful
	"""
	if self.weather_entity is None:
		_LOGGER.warning("better_thermostat: weather entity not available.")
		return None
	
	if self.off_temperature is None or not isinstance(self.off_temperature, float):
		_LOGGER.warning("better_thermostat: off_temperature not set or not a float.")
		return None
	
	try:
		forcast = self.hass.states.get(self.weather_entity).attributes.get('forecast')
		if len(forcast) > 0:
			max_forcast_temp = math.ceil((float(forcast[0]['temperature']) + float(forcast[1]['temperature'])) / 2)
			return float(max_forcast_temp) < float(self.off_temperature)
		else:
			raise TypeError
	except TypeError:
		_LOGGER.warning("better_thermostat: no weather entity data found.")
		return None

def check_ambient_air_temperature(self):
	"""
	Gets the history for two days and evaluates the necessary for heating.
	@return: returns True if the average temperature is lower than the off temperature; None if not successful
	"""
	if self.outdoor_sensor is None:
		return None
	
	if self.off_temperature is None or not isinstance(self.off_temperature, float):
		_LOGGER.warning("better_thermostat: off_temperature not set or not a float.")
		return None
	
	try:
		last_two_days_date_time = datetime.now() - timedelta(days=2)
		start = dt_util.as_utc(last_two_days_date_time)
		history_list = history.state_changes_during_period(
			self.hass, start, dt_util.as_utc(datetime.now()), self.outdoor_sensor
		)
		historic_sensor_data = history_list.get(self.outdoor_sensor)
	except TypeError:
		_LOGGER.warning("better_thermostat: no outdoor sensor data found.")
		return None
	
	# create a list from valid data in historic_sensor_data
	valid_historic_sensor_data = []
	for measurement in historic_sensor_data:
		if measurement.state is not None:
			try:
				valid_historic_sensor_data.append(float(measurement.state))
			except ValueError:
				pass
			except TypeError:
				pass
	
	# remove the upper and lower 5% of the data
	valid_historic_sensor_data.sort()
	valid_historic_sensor_data = valid_historic_sensor_data[
									int(len(valid_historic_sensor_data) * 0.05):int(len(valid_historic_sensor_data) * 0.95)]
	
	if len(valid_historic_sensor_data) == 0:
		_LOGGER.warning("better_thermostat: no valid outdoor sensor data found.")
		return None
	
	# calculate the average temperature
	avg_temp = math.ceil(sum(valid_historic_sensor_data) / len(valid_historic_sensor_data))
	_LOGGER.debug("better_thermostat: avg outdoor temp: %s", avg_temp)
	return float(avg_temp) < float(self.off_temperature)