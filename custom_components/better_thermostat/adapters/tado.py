"""Tado adapter helpers for Better Thermostat.

This module implements the thin adapter that maps Better Thermostat actions
onto the Tado climate services (offsets and modes).
"""

from __future__ import annotations

import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from .base import AdapterCapabilities
from .generic import (
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)
from .types import AdapterHost, AdapterProbeHost

_LOGGER = logging.getLogger(__name__)

# Tado: offset via the tado.set_climate_temperature_offset service,
# no valve channel.
CAPABILITIES = AdapterCapabilities(
    offset_write=True, offset_needs_entity=False, valve_write=False
)


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Get info from TRV."""
    return {"support_offset": True, "support_valve": False}


async def init(self: AdapterHost, entity_id: str) -> None:
    """Perform per-entity initialization for the Tado adapter.

    Currently, no initialization is required and the function returns None.
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
        return float(str(state.attributes.get("offset_celsius", 0)))
    except ValueError, TypeError:
        _LOGGER.warning(
            "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
            self.device_name,
            state.attributes.get("offset_celsius"),
        )
        return 0.0


async def get_offset_step(self: AdapterHost, entity_id: str) -> float:
    """Get offset step."""
    return 0.01


async def get_min_offset(self: AdapterHost, entity_id: str) -> float:
    """Get min offset."""
    return -10


async def get_max_offset(self: AdapterHost, entity_id: str) -> float:
    """Get max offset."""
    return 10


async def set_offset(self: AdapterHost, entity_id: str, offset: float) -> bool:
    """Write a calibration offset through the Tado offset service.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to
    offset : float
        Calibration offset in Kelvin, clamped to the range Tado accepts

    Returns
    -------
    bool
        True once the write went out. The offset rides on the TRV's own
        service call, so every Tado TRV has the channel.
    """
    offset = min(10, offset)
    offset = max(-10, offset)
    await self.hass.services.async_call(
        "tado",
        "set_climate_temperature_offset",
        {"entity_id": entity_id, "offset": offset},
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id].last_calibration = offset
    return True


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Set new target valve."""
    return None
