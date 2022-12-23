import logging

from custom_components.better_thermostat.const import CONF_HOMATICIP
from ..utils.helpers import convert_to_float
from datetime import datetime

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
    if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
        return

    _incoming_temperature = convert_to_float(
        str(new_state.state), self.name, "external_temperature"
    )

    _time_diff = 5

    try:
        for trv in self.all_trvs:
            if trv["advanced"][CONF_HOMATICIP]:
                _time_diff = 600
    except KeyError:
        pass

    if (
        _incoming_temperature != self.cur_temp
        and (datetime.now() - self.last_external_sensor_change).total_seconds()
        > _time_diff
    ):
        _LOGGER.debug(
            "better_thermostat %s: external_temperature changed from %s to %s",
            self.name,
            self.cur_temp,
            _incoming_temperature,
        )
        self.cur_temp = _incoming_temperature
        self.async_write_ha_state()
        await self.control_queue_task.put(self)
        self.last_external_sensor_change = datetime.now()
