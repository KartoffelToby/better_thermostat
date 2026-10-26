"""Model quirks for devices whose registry model is ``Heater``.

The ha-termoweb integration registers TermoWeb electric radiators with the
model ``Heater`` and the manufacturer ``TermoWeb``. Such a heater subtracts
its temperature offset from the temperature it measures and reports that
difference, so an offset of +1 K lowers its reading by 1 K. The offset it
holds is the negation of the one Better Thermostat works with.

``Heater`` is a generic model name other integrations may register too, so
the quirk answers for the TermoWeb manufacturer only. Every other device of
that model is driven as the default module drives it.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.better_thermostat.model_fixes.types import (
    InvertedOffsetQuirk,
    ModelFixHost,
    ModelQuirks,
    OffsetConventionHost,
)

_LOGGER = logging.getLogger(__name__)

# Manufacturer the ha-termoweb integration registers its heaters under.
TERMOWEB_MANUFACTURER: Final = "TermoWeb"


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


def local_calibration_inverted(self: OffsetConventionHost, entity_id: str) -> bool:
    """Report whether the TRV is a TermoWeb heater, which negates its offset.

    The manufacturer is read off the device the TRV's entity belongs to. An
    entity without a device, and a device the registry knows only as a child
    of another, names no manufacturer and is driven like any other device.

    Parameters
    ----------
    self : OffsetConventionHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity id of the TRV the offset is read from and written to.

    Returns
    -------
    bool
        True when the device registry names TermoWeb as the manufacturer.
    """
    entry = er.async_get(self.hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return False
    device = dr.async_get(self.hass).async_get(entry.device_id)
    return (
        isinstance(device, dr.DeviceEntry)
        and device.manufacturer == TERMOWEB_MANUFACTURER
    )


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
    """No HVAC mode override for TermoWeb heaters.

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
    """No set_temperature override for TermoWeb heaters.

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
    local_calibration_inverted = staticmethod(local_calibration_inverted)


_MODEL_QUIRKS: ModelQuirks = _Surface()
_INVERTED_OFFSET_QUIRK: InvertedOffsetQuirk = _Surface()
