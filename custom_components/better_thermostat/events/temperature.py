from datetime import datetime, timedelta
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import callback


_LOGGER = logging.getLogger(__name__)


@callback
async def trigger_temperature_change(self, event):
    """Handle temperature changes.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    event :
            Event object from the eventbus. Contains the current trigger time.

    Returns
    -------
    None
    """
    if self.startup_running:
        return
    new_state = event.data.get("new_state")
    if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return

    if self.homaticip:
        if (
            self._cur_temp is float(new_state.state)
            or ((float(self._cur_temp) - float(new_state.state)) < 1.0)
            or (self.last_change + timedelta(minutes=30)).timestamp()
            > datetime.now().timestamp()
        ):
            _async_update_temp(self, new_state)
            self.async_write_ha_state()
            _LOGGER.info(
                f"better_thermostat {self.name}: skip sending new external temp to TRV because of homaticip throttling"
            )
            return

    _async_update_temp(self, new_state)
    self.async_write_ha_state()
    await self.control_queue_task.put(self)


@callback
def _async_update_temp(self, state):
    """Update thermostat with the latest state from sensor.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    state :
            a HA state object

    Returns
    -------
    None
    """
    try:
        self._cur_temp = float(state.state)
    except (ValueError, AttributeError, KeyError, TypeError, NameError, IndexError):
        _LOGGER.error(
            "better_thermostat %s: Unable to update temperature sensor status from status update, current temperature not a number",
            self.name,
        )
