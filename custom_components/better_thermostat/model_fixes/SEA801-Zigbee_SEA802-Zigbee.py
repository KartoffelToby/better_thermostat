"""Model quirks for SEA801/SEA802 Zigbee thermostats.

Includes device-specific offsets and behavior adaptations required for certain
SEA801/SEA802 based devices.
"""

import logging

from custom_components.better_thermostat.utils.helpers import (
    entity_uses_mpc_calibration,
)

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Adjust the local calibration offset for SEA801/SEA802 devices.

    The function applies small adjustments based on the external and target
    temperatures to avoid incorrect temperature behavior.
    """
    if entity_uses_mpc_calibration(self, entity_id):
        return offset
    _cur_external_temp = self.cur_temp
    _target_temp = self.bt_target_temp

    if (_cur_external_temp + 0.1) >= _target_temp:
        offset = round(offset + 0.5, 1)
    elif (_cur_external_temp + 0.5) >= _target_temp:
        offset -= 2.5

    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Adjust the setpoint temperature for SEA801/SEA802 devices.

    Ensures a minimum distance between the current TRV temperature and the
    target temperature to avoid short-cycling and oscillation.
    """
    _state = self.hass.states.get(entity_id)
    _cur_trv_temp = None
    if _state is not None:
        _cur_trv_temp = _state.attributes.get("current_temperature")
    if _cur_trv_temp is None:
        return temperature

    if entity_uses_mpc_calibration(self, entity_id):
        return temperature

    _cur_trv_temp = float(_cur_trv_temp)
    if (
        round(temperature, 1) > round(_cur_trv_temp, 1)
        and temperature - _cur_trv_temp < 1.5
    ):
        # Statt die gewünschte Temperatur pauschal um 1.5°C zu erhöhen,
        # setze sie auf mindestens (aktuelle TRV-Temp + 1.5°C).
        # So ist der Mindestabstand garantiert, ohne unnötig zu überschießen.
        temperature = round(_cur_trv_temp + 1.5, 1)

    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No HVAC mode override for SEA801/SEA802 devices.

    Return False to indicate no custom handling and let the adapter handle
    normal behavior.
    """
    return False


async def override_set_temperature(self, entity_id, temperature):
    """No set_temperature override for SEA801/SEA802 devices.

    Return False to indicate the adapter should use the default set_temperature
    implementation.
    """
    return False
