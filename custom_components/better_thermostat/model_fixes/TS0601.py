"""Model quirks for Tuya devices that report the model TS0601.

ZHA reports the model string a Tuya device sends, which is TS0601 for nearly
every Tuya TRV, and leaves the manufacturer string (``_TZE200_…``) as the
only name that tells them apart. Zigbee2MQTT tells them apart by that string
and reports a model of their own, so a device reaches this module through
ZHA, or as a TS0601 Zigbee2MQTT has no definition for.

The Saswell SEA801/SEA802 and its white labels (Hama, HiHome, Nous and
others) need their setpoint and offset lifted to open the valve, and are
driven as ``SEA801-Zigbee_SEA802-Zigbee`` drives them under Zigbee2MQTT. The
manufacturer strings are the ones Zigbee2MQTT recognises that device by.
Every other TS0601 passes its calibration through unchanged, as the default
quirks do.
"""

from __future__ import annotations

from importlib import import_module
from typing import Final

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.better_thermostat.model_fixes.types import (
    ModelFixHost,
    ModelQuirks,
)

_SASWELL = import_module(
    "custom_components.better_thermostat.model_fixes.SEA801-Zigbee_SEA802-Zigbee"
)

_SASWELL_MANUFACTURERS: Final = frozenset(
    {
        "_TZE200_c88teujp",
        "_TZE200_yw7cahqs",
        "_TZE200_azqp6ssj",
        "_TZE200_zuhszj9s",
        "_TZE200_9gvruqf5",
        "_TZE200_zr9c0day",
        "_TZE200_0dvm9mva",
        "_TZE284_0dvm9mva",
        "_TZE200_h4cgnbzg",
        "_TZE200_gd4rvykv",
        "_TZE200_exfrnlow",
        "_TZE200_9m4kmbfu",
        "_TZE284_9m4kmbfu",
        "_TZE200_3yp57tby",
        "_TZE204_3yp57tby",
        "_TZE200_7p8ugv8d",
        "_TZE284_3yp57tby",
        "_TZE2841000000_3yp57tby",
        "_TZE200_3ymoslep",
        "_TZE204_3ymoslep",
        "_TZE284_3ymoslep",
        "_TZE2841000000_3ymoslep",
    }
)


def _is_saswell(self: ModelFixHost, entity_id: str) -> bool:
    """Answer whether the TRV is a Saswell SEA801/SEA802 or one of its white labels.

    The manufacturer is read off the device the TRV's entity belongs to. An
    entity without a device names no manufacturer and passes its calibration
    through.

    Parameters
    ----------
    self : ModelFixHost
        Host providing Home Assistant access.
    entity_id : str
        Entity id of the TRV.

    Returns
    -------
    bool
        True when the device registry names a Saswell manufacturer string.
    """
    entry = er.async_get(self.hass).async_get(entity_id)
    if entry is None or entry.device_id is None:
        return False
    device = dr.async_get(self.hass).async_get(entry.device_id)
    return (
        isinstance(device, dr.DeviceEntry)
        and device.manufacturer in _SASWELL_MANUFACTURERS
    )


def fix_local_calibration(
    self: ModelFixHost, entity_id: str, calibration_offset: float
) -> float:
    """Adjust the local calibration offset of a Saswell head, pass any other through.

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
        The offset the Saswell quirk answers, or the offset unchanged.
    """
    if _is_saswell(self, entity_id):
        return _SASWELL.fix_local_calibration(self, entity_id, calibration_offset)
    return calibration_offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Adjust the setpoint of a Saswell head, pass any other through.

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
        The setpoint the Saswell quirk answers, or the setpoint unchanged.
    """
    if _is_saswell(self, entity_id):
        return _SASWELL.fix_target_temperature_calibration(self, entity_id, temperature)
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """No special HVAC mode override for TS0601 devices.

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
    """No special set_temperature override for TS0601 devices.

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
