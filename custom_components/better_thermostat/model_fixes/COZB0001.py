"""Model quirks for the Eurotronic Comet Zigbee (COZB0001).

The COZB0001 adds its local temperature calibration to the setpoint, not to
the temperature it measures, and keeps reporting the measured temperature
without it. The predecessor SPZB0001 offsets the reading like most devices.
"""

from __future__ import annotations

import logging

from custom_components.better_thermostat.model_fixes.types import (
    ModelFixHost,
    ModelQuirks,
    SetpointOffsetQuirk,
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


def local_calibration_shifts_setpoint(self: ModelFixHost, entity_id: str) -> bool:
    """Report that the COZB0001 adds its calibration offset to the setpoint.

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
    """No HVAC mode override for COZB0001 devices.

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
    """No set_temperature override for COZB0001 devices.

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
    local_calibration_shifts_setpoint = staticmethod(local_calibration_shifts_setpoint)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_SETPOINT_OFFSET_QUIRK: SetpointOffsetQuirk = _Surface()
