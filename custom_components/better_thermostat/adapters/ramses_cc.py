"""RAMSES RF adapter for Honeywell Evohome heating zones."""

from __future__ import annotations

import logging

from .base import AdapterCapabilities, DeviceChannels
from .generic import (
    get_calibration_offset,
    get_calibration_offset_step,
    get_max_calibration_offset,
    get_min_calibration_offset,
    init,
    set_calibration_offset,
    set_hvac_mode,
    set_valve,
)
from .types import AdapterHost, AdapterProbeHost

__all__ = (
    "CAPABILITIES",
    "get_calibration_offset",
    "get_calibration_offset_step",
    "get_info",
    "get_max_calibration_offset",
    "get_min_calibration_offset",
    "init",
    "set_calibration_offset",
    "set_hvac_mode",
    "set_temperature",
    "set_valve",
)

_LOGGER = logging.getLogger(__name__)

CAPABILITIES = AdapterCapabilities()


async def get_info(self: AdapterProbeHost, entity_id: str) -> DeviceChannels:
    """Report that Evohome zones offer only target-temperature control."""
    return DeviceChannels()


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Apply a permanent Evohome zone override with a Celsius setpoint."""
    _LOGGER.debug(
        "Setting permanent zone override for %s to %.1f °C", entity_id, temperature
    )
    await self.hass.services.async_call(
        "ramses_cc",
        "set_zone_mode",
        {"entity_id": entity_id, "mode": "permanent_override", "setpoint": temperature},
        blocking=True,
        context=self.context,
    )
