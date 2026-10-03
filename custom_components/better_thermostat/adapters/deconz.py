"""Adapter for deCONZ devices.

This module implements the minimal adapter interface required by the
Better Thermostat integration for deCONZ-controlled TRV devices.
"""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from .base import AdapterCapabilities
from .generic import (
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)
from .types import AdapterHost, AdapterProbeHost

_LOGGER = logging.getLogger(__name__)

# deCONZ: offset via the deconz.configure service, no valve channel.
CAPABILITIES = AdapterCapabilities(
    offset_write=True, offset_needs_entity=False, valve_write=False
)

# The offset range and granularity the deCONZ configure service accepts.
# They belong to the ecosystem rather than to a discovered entity, so every
# deCONZ TRV reports the same three numbers and the write clamps to them.
OFFSET_MIN: Final = -6.0
OFFSET_MAX: Final = 6.0
OFFSET_STEP: Final = 1.0

# deCONZ expresses a thermostat's ``config/offset`` in hundredths of a degree,
# the same encoding it uses for ``heatsetpoint`` and the measured temperature:
# the value 250 is 2.5 K. Every number crossing this adapter's own interface is
# in Kelvin, so the wire value is scaled on the way out and back on the way in.
OFFSET_UNITS_PER_KELVIN: Final = 100


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Get info from TRV."""
    state = self.hass.states.get(entity_id)
    if state is None:
        return {"support_offset": False, "support_valve": False}
    _offset = state.attributes.get("offset", None)
    if _offset is None:
        return {"support_offset": False, "support_valve": False}
    return {"support_offset": True, "support_valve": False}


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize adapter for an entity.

    This adapter does not require any special initialization, so the
    function returns None.
    """
    return None


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


async def get_current_offset(self: AdapterHost, entity_id: str) -> float:
    """Get current offset."""
    state = self.hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return 0.0
    try:
        return float(str(state.attributes.get("offset", 0))) / OFFSET_UNITS_PER_KELVIN
    except ValueError, TypeError:
        _LOGGER.warning(
            "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
            self.device_name,
            state.attributes.get("offset"),
        )
        return 0.0


async def get_offset_step(self: AdapterHost, entity_id: str) -> float:
    """Get offset step."""
    return OFFSET_STEP


async def get_min_offset(self: AdapterHost, entity_id: str) -> float:
    """Get min offset."""
    return OFFSET_MIN


async def get_max_offset(self: AdapterHost, entity_id: str) -> float:
    """Get max offset."""
    return OFFSET_MAX


async def set_offset(
    self: AdapterHost, entity_id: str, calibration_offset: float
) -> bool:
    """Write a calibration offset through the deCONZ configure service.

    deCONZ counts the offset in hundredths of a Kelvin, so the clamped Kelvin
    value is scaled by ``OFFSET_UNITS_PER_KELVIN`` here. The ``configure``
    service forwards that payload to the REST API unaltered.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to
    calibration_offset : float
        Calibration offset in Kelvin, clamped to the range deCONZ accepts

    Returns
    -------
    bool
        True once the write went out. The offset rides on the TRV's own
        service call, so every deCONZ TRV has the channel.
    """
    calibration_offset = min(await get_max_offset(self, entity_id), calibration_offset)
    calibration_offset = max(await get_min_offset(self, entity_id), calibration_offset)
    await self.hass.services.async_call(
        "deconz",
        "configure",
        {
            "entity": entity_id,
            "field": "/config",
            "data": {"offset": round(calibration_offset * OFFSET_UNITS_PER_KELVIN)},
        },
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id].last_calibration = calibration_offset
    return True


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Set new target valve."""
    return None
