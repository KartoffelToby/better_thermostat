"""Model quirks for SEA801/SEA802 Zigbee thermostats.

Includes device-specific offsets and behavior adaptations required for certain
SEA801/SEA802 based devices.
"""

from __future__ import annotations

import logging

from custom_components.better_thermostat.model_fixes.types import ModelFixHost
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float_celsius,
    entity_uses_mpc_calibration,
    state_temperature_unit,
)

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Adjust the local calibration offset for SEA801/SEA802 devices.

    The function applies small adjustments based on the external and target
    temperatures to avoid incorrect temperature behavior; without either of
    them the offset is returned unchanged.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV the offset belongs to.
    calibration_offset : float
        Local calibration offset reported by the device.

    Returns
    -------
    float
        The adjusted local calibration offset.
    """
    if entity_uses_mpc_calibration(self, entity_id):
        return calibration_offset
    _cur_external_temp = self.room_temperature
    _heat_target_temperature = self.heat_target_temperature

    if _cur_external_temp is None or _heat_target_temperature is None:
        return calibration_offset

    if (_cur_external_temp + 0.1) >= _heat_target_temperature:
        calibration_offset = round(calibration_offset + 0.5, 1)
    elif (_cur_external_temp + 0.5) >= _heat_target_temperature:
        calibration_offset -= 2.5

    return calibration_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Adjust the setpoint temperature for SEA801/SEA802 devices.

    Ensures a minimum distance between the current TRV temperature and the
    target temperature to avoid short-cycling and oscillation.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV whose setpoint is calibrated.
    temperature : float
        Requested setpoint temperature.

    Returns
    -------
    float
        The adjusted setpoint temperature.
    """
    _state = self.hass.states.get(entity_id)
    _cur_trv_temp = None
    if _state is not None and _state.attributes.get("current_temperature") is not None:
        # A climate entity reports in the system unit; the setpoint is °C.
        _cur_trv_temp = convert_to_float_celsius(
            _state.attributes.get("current_temperature"),
            self.device_name,
            "fix_target_temperature_calibration",
            state_temperature_unit(
                _state.attributes, self.hass.config.units.temperature_unit
            ),
        )
    if _cur_trv_temp is None:
        return temperature

    if entity_uses_mpc_calibration(self, entity_id):
        return temperature

    if (
        round(temperature, 1) > round(_cur_trv_temp, 1)
        and temperature - _cur_trv_temp < 1.5
    ):
        # Instead of bumping the target temperature by a flat 1.5°C,
        # set it to at least (current TRV temp + 1.5°C).
        # This guarantees the minimum gap without overshooting unnecessarily.
        temperature = round(_cur_trv_temp + 1.5, 1)

    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No HVAC mode override for SEA801/SEA802 devices.

    Return False to indicate no custom handling and let the adapter handle
    normal behavior.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV.
    hvac_mode : str
        Requested HVAC mode.

    Returns
    -------
    bool
        True if the model handled the change, otherwise False.
    """
    return False


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """No set_temperature override for SEA801/SEA802 devices.

    Return False to indicate the adapter should use the default set_temperature
    implementation.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV.
    temperature : float
        Requested setpoint temperature.

    Returns
    -------
    bool
        True if the model handled the change, otherwise False.
    """
    return False
