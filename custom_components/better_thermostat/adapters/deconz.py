"""Adapter for deCONZ devices.

This module implements the minimal adapter interface required by the
Better Thermostat integration for deCONZ-controlled TRV devices.
"""

import logging
from .generic import (
    set_temperature as generic_set_temperature,
    set_hvac_mode as generic_set_hvac_mode,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Get info from TRV."""
    _offset = self.hass.states.get(entity_id).attributes.get("offset", None)
    if _offset is None:
        return {"support_offset": False, "support_valve": False}
    return {"support_offset": True, "support_valve": False}


async def init(self, entity_id):
    """Initialize adapter for an entity.

    This adapter does not require any special initialization, so the
    function returns None.
    """
    return None


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


async def get_current_offset(self, entity_id):
    """Get current offset."""
    state = self.hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return 0.0
    try:
        return float(str(state.attributes.get("offset", 0)))
    except (ValueError, TypeError):
        _LOGGER.warning(
            "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
            self.device_name,
            state.attributes.get("offset"),
        )
        return 0.0


async def get_offset_step(self, entity_id):
    """Get offset step."""
    return float(1.0)


async def get_min_offset(self, entity_id):
    """Get min offset."""
    return -6


async def get_max_offset(self, entity_id):
    """Get max offset."""
    return 6


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    await self.hass.services.async_call(
        "deconz",
        "configure",
        {"entity": entity_id, "field": "/config", "data": {"offset": offset}},
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id]["last_calibration"] = offset


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    return None
