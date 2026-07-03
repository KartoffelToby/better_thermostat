"""MQTT adapter for TRV devices.

This module implements MQTT-specific behaviour for TRV devices used by
the Better Thermostat integration.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.climate.const import PRESET_NONE
from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import find_local_calibration_entity, find_valve_entity
from .base import wait_for_calibration_entity_or_timeout
from .generic import (
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)

_LOGGER = logging.getLogger(__name__)


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
        _preset_modes = (
            state.attributes.get("preset_modes") if state is not None else None
        )
        # Only reset the device preset when "none" is actually a supported mode.
        # Some TRVs (e.g. proportional Tuya models) expose ``preset_modes`` but
        # do not accept arbitrary values, so calling the service with an
        # unsupported mode raises ``ServiceValidationError`` and trips the
        # startup retry loop.
        if _preset_modes and PRESET_NONE in _preset_modes:
            await self.hass.services.async_call(
                "climate",
                "set_preset_mode",
                {"entity_id": entity_id, "preset_mode": PRESET_NONE},
                blocking=True,
                context=self.context,
            )
        elif _preset_modes:
            _LOGGER.debug(
                "better_thermostat %s: TRV %s supports presets %s but not '%s'; "
                "skipping preset reset",
                self.device_name,
                entity_id,
                _preset_modes,
                PRESET_NONE,
            )


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    await generic_set_hvac_mode(self, entity_id, hvac_mode)
    await asyncio.sleep(3)


async def get_current_offset(self, entity_id):
    """Get current offset."""
    state = self.hass.states.get(
        self.real_trvs[entity_id].local_temperature_calibration_entity
    )
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


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    max_calibration = await get_max_offset(self, entity_id)
    min_calibration = await get_min_offset(self, entity_id)

    offset = min(max_calibration, offset)
    offset = max(min_calibration, offset)

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {
            "entity_id": self.real_trvs[entity_id].local_temperature_calibration_entity,
            "value": offset,
        },
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id].last_calibration = offset
    if (
        self.real_trvs[entity_id].last_hvac_mode is not None
        and self.real_trvs[entity_id].last_hvac_mode != "off"
    ):
        await asyncio.sleep(3)
        return await generic_set_hvac_mode(
            self, entity_id, self.real_trvs[entity_id].last_hvac_mode
        )


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

    # get min max from entity attributes
    valve_entity = self.hass.states.get(self.real_trvs[entity_id].valve_position_entity)
    if valve_entity is not None:
        min_valve = float(str(valve_entity.attributes.get("min", 0)))
        max_valve = float(str(valve_entity.attributes.get("max", 100)))
        valve = min_valve + (valve / 100.0) * (max_valve - min_valve)
        step = float(str(valve_entity.attributes.get("step", 1)))
        valve = round(valve / step) * step

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": self.real_trvs[entity_id].valve_position_entity, "value": valve},
        blocking=True,
        context=self.context,
    )
