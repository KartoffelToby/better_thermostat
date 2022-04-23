import logging

from homeassistant.core import callback


_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_time(self, current_time):
    """Triggered by night mode timer.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    current_time :
            Event object from the eventbus. Contains the current trigger time.

    Returns
    -------
    None
    """
    _is_night = _nighttime(self, current_time)

    if _is_night is None:
        _LOGGER.error(
            "better_thermostat %s: Error while checking if it is night", self.name
        )
        return
    elif _is_night:
        _LOGGER.debug("better_thermostat %s: Night mode activated", self.name)
        self.last_daytime_temp = self._target_temp
        self._target_temp = self.night_temp
        self.night_mode_active = True

    else:
        _LOGGER.debug("ai_thermostat %s: Day mode activated", self.name)
        if self.last_daytime_temp is None:
            _LOGGER.error(
                "better_thermostat %s: Could not load last daytime temp; continue using the current setpoint",
                self.name,
            )
        else:
            self._target_temp = self.last_daytime_temp
        self.night_mode_active = False

    self.async_write_ha_state()
    await self.control_queue_task.put(self)


@callback
def _nighttime(self, current_time):
    """Checks whether it is currently nighttime

    Parameters
    ----------
    self :
            self instance of better_thermostat
    current_time :
            Event object from the eventbus. Contains the current trigger time.

    Returns
    -------
    bool
            True if it is nighttime
    None
            if not configured
    """
    _return_value = None

    # one or more of the inputs is None or empty
    if None in (self.night_start, self.night_end, current_time):
        return _return_value

    if (
        self.night_start.hour == current_time.hour
        and self.night_start.minute == current_time.minute
    ):
        _return_value = True

    if (
        self.night_end.hour == current_time.hour
        and self.night_end.minute == current_time.minute
    ):
        _return_value = False

    return _return_value
