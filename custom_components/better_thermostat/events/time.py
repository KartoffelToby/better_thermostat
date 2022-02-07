import logging

from ..controlling import control_trv
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)

@callback
async def trigger_time(self, current_time):
    """
    Triggered by night mode timer.
    @param current_time:
    """
    
    _is_night = _nighttime(current_time)
    
    if _is_night is None:
        _LOGGER.error("better_thermostat %s: Error while checking if it is night", self.name)
        return
    elif _is_night:
        _LOGGER.debug("better_thermostat %s: Night mode activated", self.name)
        self.last_daytime_temp = self._target_temp
        self._target_temp = self.night_temp
        self.night_mode_active = True
    
    else:
        _LOGGER.debug("ai_thermostat %s: Day mode activated", self.name)
        if self.last_daytime_temp is None:
            _LOGGER.error("better_thermostat %s: Could not load last daytime temp; continue using the current setpoint", self.name)
        else:
            self._target_temp = self.last_daytime_temp
        self.night_mode_active = False
    
    self.async_write_ha_state()
    await control_trv(self)

@callback
def _nighttime(self, current_time):
    """
    Return whether it is nighttime.
    @param current_time: time.time()
    @return: bool True if it is nighttime; None if not configured
    """
    _return_value = None
    
    # one or more of the inputs is None or empty
    if not all([self.night_start, self.night_end, current_time]):
        return _return_value
    
    # fetch to instance variables, since we might want to swap them
    start_time, end_time = self.night_start, self.night_end
    
    # if later set to true we'll swap the variables and output boolean, 'cause we use the logic backwards
    #   if the nighttime passes not over midnight, like (01:00 to 05:00) we use the inverted logic
    #   while something like 23:00 to 05:00 would use the default
    _reverse = False
    
    if start_time.hour < end_time.hour or (start_time.hour == end_time.hour and start_time.minute < end_time.minute):
        # not passing midnight, so we use the inverted logic
        _reverse = True
        start_time, end_time = end_time, start_time
    
    # if we are after the start time, but before the end time, we are in the night
    if (current_time.hour > start_time.hour or (
            current_time.hour == start_time.hour and current_time.minute >= start_time.minute)) and current_time.hour < end_time.hour or (
            current_time.hour == end_time.hour and current_time.minute < end_time.minute):
        _return_value = True
    
    # flip output, since we flipped the start/end time
    if _reverse:
        return not _return_value
    return _return_value