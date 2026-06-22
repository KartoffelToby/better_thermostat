"""Model quirks for ME167 Zigbee thermostats.

Includes device-specific offsets and behavior adaptations required for certain
ME167 based devices.
"""

import logging

from custom_components.better_thermostat.model_fixes.types import ModelFixHost

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self: ModelFixHost, entity_id: str, offset: float) -> float:
    """Adjust the local calibration offset for ME167 devices.

    Just invert the given offset for this model, as they seem to report it in
    reverse compared to other devices.

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
        The inverted local calibration offset.
    """
    return -offset


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
