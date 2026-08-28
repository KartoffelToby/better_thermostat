"""MQTT adapter for TRV devices.

This module implements MQTT-specific behaviour for TRV devices used by
the Better Thermostat integration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import find_local_calibration_entity, find_valve_entity
from .base import AdapterCapabilities
from .generic import (
    discover_calibration_entity,
    get_max_offset as generic_get_max_offset,
    get_min_offset as generic_get_min_offset,
    get_offset_step as generic_get_offset_step,
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)
from .types import AdapterHost, AdapterProbeHost
from .valve_entity import discover_valve_entity, write_valve_percent

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


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Get info from TRV."""
    support_offset = False
    support_valve = False
    offset = await find_local_calibration_entity(self, entity_id)
    if offset is not None:
        support_offset = True
    valve = await find_valve_entity(self, entity_id)
    if valve is not None and valve.get("entity_id"):
        support_valve = bool(valve.get("writable", False))
    return {"support_offset": support_offset, "support_valve": support_valve}


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the MQTT adapter for a TRV entity.

    Adopts the valve position entity and the local calibration entity,
    and takes the TRV off its own schedule on the pass that looks the
    calibration entity up.
    """
    await discover_valve_entity(self, entity_id)

    trv = self.real_trvs[entity_id]
    # The preset reset rides on the pass that looks the calibration entity
    # up: a TRV that already carries one, and one that is not calibrated
    # through such an entity, keep the preset they are on.
    resets_preset = (
        trv.local_temperature_calibration_entity is None and trv.calibration != 1
    )

    await discover_calibration_entity(self, entity_id)

    if not resets_preset:
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


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Set new target hvac mode."""
    await generic_set_hvac_mode(self, entity_id, hvac_mode)
    await asyncio.sleep(3)


async def get_current_offset(self: AdapterHost, entity_id: str) -> float:
    """Get current offset."""
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is None:
        return 0.0
    state = self.hass.states.get(calibration_entity)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return 0.0
    try:
        return float(str(state.state))
    except ValueError, TypeError:
        _LOGGER.warning(
            "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
            self.device_name,
            state.state,
        )
        return 0.0


async def get_offset_step(self: AdapterHost, entity_id: str) -> float:
    """Get offset step."""
    return await generic_get_offset_step(self, entity_id)


async def get_min_offset(self: AdapterHost, entity_id: str) -> float:
    """Get min offset."""
    return await generic_get_min_offset(self, entity_id)


async def get_max_offset(self: AdapterHost, entity_id: str) -> float:
    """Get max offset."""
    return await generic_get_max_offset(self, entity_id)


async def set_offset(self: AdapterHost, entity_id: str, offset: float) -> bool:
    """Write a calibration offset to the discovered calibration entity.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to
    offset : float
        Calibration offset in Kelvin, clamped to the device's declared range

    Returns
    -------
    bool
        True once the write went out, False when no calibration entity was
        discovered for this TRV and there is nothing to write to.
    """
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is None:
        return False

    max_calibration = await get_max_offset(self, entity_id)
    min_calibration = await get_min_offset(self, entity_id)

    offset = min(max_calibration, offset)
    offset = max(min_calibration, offset)

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": calibration_entity, "value": offset},
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id].last_calibration = offset
    last_hvac_mode = self.real_trvs[entity_id].last_hvac_mode
    if last_hvac_mode is not None and last_hvac_mode != "off":
        await asyncio.sleep(3)
        await generic_set_hvac_mode(self, entity_id, last_hvac_mode)
    return True


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Set new target valve."""
    await write_valve_percent(self, entity_id, valve)
