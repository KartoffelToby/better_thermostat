"""Quirks for the Shelly TRV (Gen1, SHTRV-01).

Home Assistant's Shelly integration has no mode of its own for this TRV. It
reports the TRV as off whenever the setpoint sits at the minimum of 4 °C,
and switching the TRV off writes that minimum. A setpoint Better Thermostat
writes at the minimum to close the valve therefore reads back as the TRV
being switched off, and Better Thermostat would adopt that as the user
turning the room off. The lowest setpoint written is one step of the
device's 0.5 °C grid above the minimum, which still closes the valve.

Everything else stays as the default quirks have it.
"""

from __future__ import annotations

from custom_components.better_thermostat.model_fixes.default import (
    fix_local_calibration,
    fix_target_temperature_calibration,
    initial_tweak,
    override_set_hvac_mode,
    override_set_temperature,
)
from custom_components.better_thermostat.model_fixes.types import ModelFixHost

# The setpoint grid Home Assistant's Shelly integration publishes for the TRV.
_SETPOINT_STEP = 0.5

__all__ = [
    "fix_local_calibration",
    "fix_target_temperature_calibration",
    "initial_tweak",
    "lowest_setpoint",
    "override_set_hvac_mode",
    "override_set_temperature",
]


def lowest_setpoint(self: ModelFixHost, entity_id: str, min_temp: float) -> float:
    """Return the lowest setpoint that does not read back as off.

    Parameters
    ----------
    self :
        self instance of better_thermostat
    entity_id : str
        Entity id of the TRV
    min_temp : float
        Minimum setpoint the TRV publishes, in Celsius

    Returns
    -------
    float
        One grid step above ``min_temp``
    """
    return min_temp + _SETPOINT_STEP
