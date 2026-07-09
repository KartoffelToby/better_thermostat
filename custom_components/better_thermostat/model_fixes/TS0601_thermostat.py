"""Model quirks for TS0601 thermostat devices.

These helpers fix or adapt device-reported values for TS0601 thermostat
devices used by the Better Thermostat integration.
"""

from __future__ import annotations

from custom_components.better_thermostat.model_fixes.types import ModelFixHost


def fix_local_calibration(self: ModelFixHost, entity_id: str, offset: float) -> float:
    """Normalize a local calibration offset for TS0601 thermostat devices.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV the offset belongs to.
    offset : float
        Local calibration offset reported by the device.

    Returns
    -------
    float
        The adjusted local calibration offset.
    """
    _cur_external_temp = self.cur_temp
    _target_temp = self.bt_target_temp

    if (_cur_external_temp + 0.1) >= _target_temp:
        offset = round(offset + 0.5, 1)
    elif (_cur_external_temp + 0.5) >= _target_temp:
        offset -= 2.5

    return offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Adjust the target temperature for TS0601 thermostat devices.

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
    if _state is not None:
        _cur_trv_temp = _state.attributes.get("current_temperature")
    if _cur_trv_temp is None:
        return temperature
    _cur_trv_temp = float(_cur_trv_temp)
    if (
        round(temperature, 1) > round(_cur_trv_temp, 1)
        and temperature - _cur_trv_temp < 1.5
    ):
        temperature += 1.5

    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No special override for HVAC mode on TS0601 thermostats.

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
    """No special override for target temperature on TS0601 thermostats.

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
