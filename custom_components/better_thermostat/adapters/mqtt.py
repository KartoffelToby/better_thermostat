"""MQTT adapter for TRV devices.

This module implements MQTT-specific behaviour for TRV devices used by
the Better Thermostat integration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from .base import AdapterCapabilities
from .generic import (
    discover_calibration_entity,
    get_calibration_offset,
    get_calibration_offset_step,
    get_max_calibration_offset,
    get_min_calibration_offset,
    set_calibration_offset,
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature,
)
from .types import AdapterHost
from .valve_entity import (
    discover_valve_entity,
    find_entity_channels as get_info,
    write_valve_percent,
)

__all__ = (
    "CAPABILITIES",
    "get_calibration_offset",
    "get_info",
    "get_max_calibration_offset",
    "get_min_calibration_offset",
    "get_calibration_offset_step",
    "init",
    "set_hvac_mode",
    "set_calibration_offset",
    "set_temperature",
    "set_valve",
)

_LOGGER = logging.getLogger(__name__)

# Zigbee2MQTT: offset and valve position via discovered number entities.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=True)

# The preset a device offers for "the setpoint is whatever was written to me",
# tried in order. Home Assistant's MQTT climate entity inserts ``none`` into
# ``preset_modes`` itself and rejects a discovery config that lists it, so
# every device reached through this adapter offers ``none`` and none of them
# implements it: publishing it leaves the device on its schedule and the
# broker rejects the command.
MANUAL_PRESET_PREFERENCE: Final = ("manual",)


def manual_preset(preset_modes: object) -> str | None:
    """Return the preset that hands the setpoint to BT, or ``None``.

    Parameters
    ----------
    preset_modes : object
        The presets the device reports, as read from its state attributes.
        Anything that is not a sequence of presets means the device names
        none, which is the same answer as a sequence holding nothing usable.

    Returns
    -------
    str | None
        The device's own spelling of the first preset in
        ``MANUAL_PRESET_PREFERENCE`` it offers, or ``None`` when it offers
        no preset that hands the setpoint over.
    """
    if isinstance(preset_modes, str) or not isinstance(
        preset_modes, (list, tuple, set)
    ):
        return None
    offered = {str(mode).casefold(): str(mode) for mode in preset_modes}
    for candidate in MANUAL_PRESET_PREFERENCE:
        if candidate in offered:
            return offered[candidate]
    return None


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the MQTT adapter for a TRV entity.

    Adopts the valve position entity and the local calibration entity,
    and takes the TRV off its own schedule.
    """
    await discover_valve_entity(self, entity_id)

    trv = self.real_trvs[entity_id]
    # Read before the lookup below fills the entity in, so it describes the
    # record as startup built it: no calibration entity yet, and the
    # calibration type the configuration named. Only a configuration naming
    # none leaves that type at None, so the reset reaches every configured TRV,
    # whichever way that TRV is calibrated.
    resets_preset = (
        trv.local_temperature_calibration_entity is None and trv.calibration is not None
    )

    await discover_calibration_entity(self, entity_id)

    # The discovery can wait for a while, and the devices can be released
    # meanwhile; the preset is not sent to them then.
    if not resets_preset or self.devices_released:
        return

    state = self.hass.states.get(entity_id)
    attributes = state.attributes if state is not None else {}
    _preset_modes = attributes.get("preset_modes")
    # BT owns the setpoint, so a device running its own schedule has to be
    # taken off it. The device's manual preset is what does that, and it is
    # the only value here the device is known to accept.
    _manual = manual_preset(_preset_modes)
    if _manual is None:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s offers no manual preset among %s, "
            "leaving its preset alone",
            self.device_name,
            entity_id,
            _preset_modes,
        )
    elif attributes.get("preset_mode") == _manual:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s already runs preset '%s'",
            self.device_name,
            entity_id,
            _manual,
        )
    else:
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": _manual},
            blocking=True,
            context=self.context,
        )


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Set new target hvac mode."""
    await generic_set_hvac_mode(self, entity_id, hvac_mode)
    await asyncio.sleep(3)


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Set new target valve."""
    await write_valve_percent(self, entity_id, valve)
