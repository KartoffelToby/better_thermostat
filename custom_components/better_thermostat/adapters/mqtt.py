"""MQTT adapter for TRV devices.

This module implements MQTT-specific behaviour for TRV devices used by
the Better Thermostat integration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from homeassistant.components.number.const import SERVICE_SET_VALUE

from ..utils.helpers import find_local_calibration_entity, find_valve_entity
from .base import wait_for_calibration_entity_or_timeout
from .generic import (
    get_current_offset as generic_get_current_offset,
    set_hvac_mode as generic_set_hvac_mode,
    set_offset as generic_set_offset,
    set_temperature as generic_set_temperature,
)

_LOGGER = logging.getLogger(__name__)


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


async def get_info(self, entity_id):
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


async def init(self, entity_id):
    """Initialize the MQTT adapter for a TRV entity.

    Performs early discovery of the valve position and the local
    calibration entity when available.
    """
    # Try to discover valve position entity early
    try:
        from ..utils.helpers import find_valve_entity as _find_valve

        valve = await _find_valve(self, entity_id)
        if valve is not None:
            self.real_trvs[entity_id].valve_position_entity = valve.get("entity_id")
            self.real_trvs[entity_id].valve_position_writable = bool(
                valve.get("writable", False)
            )
    except Exception:
        pass

    if (
        self.real_trvs[entity_id].local_temperature_calibration_entity is None
        and self.real_trvs[entity_id].calibration != 1
    ):
        self.real_trvs[
            entity_id
        ].local_temperature_calibration_entity = await find_local_calibration_entity(
            self, entity_id
        )
        _LOGGER.debug(
            "better_thermostat %s: uses local calibration entity %s",
            self.device_name,
            self.real_trvs[entity_id].local_temperature_calibration_entity,
        )
        await wait_for_calibration_entity_or_timeout(
            self,
            entity_id,
            self.real_trvs[entity_id].local_temperature_calibration_entity,
        )

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


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    await generic_set_hvac_mode(self, entity_id, hvac_mode)
    await asyncio.sleep(3)


async def get_current_offset(self, entity_id):
    """Read the offset the calibration entity currently reports.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
    entity_id : str
        Entity ID of the TRV to read for

    Returns
    -------
    float
        Offset in Kelvin, 0.0 when the TRV has no calibration entity or the
        entity reports nothing readable.
    """
    return await generic_get_current_offset(self, entity_id)


async def get_offset_step(self, entity_id):
    """Get offset step."""
    state = self.hass.states.get(
        self.real_trvs[entity_id].local_temperature_calibration_entity
    )
    if state is None:
        return 1.0
    return float(str(state.attributes.get("step", 1)))


async def get_min_offset(self, entity_id):
    """Get min offset."""
    state = self.hass.states.get(
        self.real_trvs[entity_id].local_temperature_calibration_entity
    )
    if state is None:
        return -10.0
    return float(str(state.attributes.get("min", -10)))


async def get_max_offset(self, entity_id):
    """Get max offset."""
    state = self.hass.states.get(
        self.real_trvs[entity_id].local_temperature_calibration_entity
    )
    if state is None:
        return 10.0
    return float(str(state.attributes.get("max", 10)))


async def set_offset(self, entity_id, offset) -> bool:
    """Write a calibration offset to the discovered calibration entity.

    Parameters
    ----------
    self : BetterThermostat
        The Better Thermostat climate entity instance
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
    return await generic_set_offset(self, entity_id, offset)


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    _LOGGER.debug(
        "better_thermostat %s: TO TRV %s set_valve: %s",
        self.device_name,
        entity_id,
        valve,
    )
    trv = self.real_trvs.get(entity_id)
    if trv is not None and trv.valve_position_writable is False:
        _LOGGER.debug(
            "better_thermostat %s: valve entity for %s is read-only, skip adapter write",
            self.device_name,
            entity_id,
        )
        return

    valve_entity_id = self.real_trvs[entity_id].valve_position_entity
    if not valve_entity_id:
        return

    # Scale the 0-100 % request onto the number entity's own min/max/step,
    # clamping both the incoming percentage and the quantized result so a
    # rounding step or an out-of-range input never leaves the entity's bounds.
    # The step grid starts at the entity's minimum rather than at zero, so a
    # non-zero minimum still yields a value the entity itself offers.
    valve_entity = self.hass.states.get(valve_entity_id)
    if valve_entity is None:
        _LOGGER.debug(
            "better_thermostat %s: valve entity %s for %s reports no state, "
            "so its bounds are unknown, skip adapter write",
            self.device_name,
            valve_entity_id,
            entity_id,
        )
        return

    min_valve = float(str(valve_entity.attributes.get("min", 0)))
    max_valve = float(str(valve_entity.attributes.get("max", 100)))
    pct = max(0.0, min(100.0, valve))
    valve = min_valve + (pct / 100.0) * (max_valve - min_valve)
    step = float(str(valve_entity.attributes.get("step", 1)))
    if step > 0:
        valve = min_valve + round((valve - min_valve) / step) * step
    valve = max(min_valve, min(max_valve, valve))

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": valve_entity_id, "value": valve},
        blocking=True,
        context=self.context,
    )
