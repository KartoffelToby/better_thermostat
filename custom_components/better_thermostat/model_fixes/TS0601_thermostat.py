"""Model quirks for TS0601 thermostat devices.

These helpers fix or adapt device-reported values for TS0601 thermostat
devices used by the Better Thermostat integration.
"""

from __future__ import annotations

from custom_components.better_thermostat.model_fixes.types import (
    ModelFixHost,
    ModelQuirks,
)
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float_celsius,
    state_temperature_unit,
)


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Normalize a local calibration offset for TS0601 thermostat devices.

    The adjustment compares the room temperature against the setpoint;
    without either of them the offset is returned unchanged.

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


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)


_MODEL_QUIRKS: ModelQuirks = _Surface()
