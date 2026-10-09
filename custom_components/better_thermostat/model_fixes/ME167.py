"""Model quirks for the AVATTO ME167 Zigbee thermostat.

The ME167 applies its local temperature calibration with the opposite sign
of most devices: it subtracts the offset from the temperature it measures,
reports that difference, and reports the offset as it was written.
"""

from __future__ import annotations

import logging

from custom_components.better_thermostat.model_fixes.types import (
    ModelFixHost,
    ModelQuirks,
    ReversedOffsetQuirk,
)

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Return the given local calibration offset unchanged.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV the offset belongs to.
    calibration_offset : float
        Local calibration offset to write.

    Returns
    -------
    float
        The unchanged local calibration offset.
    """
    return calibration_offset


def local_calibration_reverses_sign(self: ModelFixHost, entity_id: str) -> bool:
    """Report that the ME167 subtracts its calibration offset from its reading.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV the offset is written to.

    Returns
    -------
    bool
        Always True.
    """
    return True


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return the given target temperature unchanged.

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
        The unchanged setpoint temperature.
    """
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No HVAC mode override for ME167 devices.

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
    """No set_temperature override for ME167 devices.

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


class _Surface:
    """Quirk surface of the module, bound below to each Protocol it implements."""

    fix_local_calibration = staticmethod(fix_local_calibration)
    fix_target_temperature_calibration = staticmethod(
        fix_target_temperature_calibration
    )
    override_set_hvac_mode = staticmethod(override_set_hvac_mode)
    override_set_temperature = staticmethod(override_set_temperature)
    local_calibration_reverses_sign = staticmethod(local_calibration_reverses_sign)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_REVERSED_OFFSET_QUIRK: ReversedOffsetQuirk = _Surface()
