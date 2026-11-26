"""RAMSES RF adapter for Honeywell Evohome heating zones."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..utils.helpers import state_is_live
from ..utils.retry import WriteWithheldError
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

CAPABILITIES = AdapterCapabilities(offset_write=False, valve_write=False)


def validate_zone(hass: HomeAssistant, entity_id: str) -> tuple[float, float]:
    """Validate RAMSES zone and return Celsius bounds."""
    entry = er.async_get(hass).async_get(entity_id)
    state = hass.states.get(entity_id)

    if (
        entry is None
        or entry.platform != "ramses_cc"
        or state is None
        or state.domain != "climate"
        or state.attributes.get("zone_index") is None
    ):
        raise HomeAssistantError(f"{entity_id} is not a RAMSES heating zone")

    config = state.attributes.get("config")
    if not isinstance(config, Mapping):
        raise HomeAssistantError(f"{entity_id} has no zone configuration")

    try:
        low = float(config["min_temp"])
        high = float(config["max_temp"])
    except (KeyError, TypeError, ValueError) as err:
        raise HomeAssistantError(f"{entity_id} has invalid temperature bounds") from err

    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise HomeAssistantError(f"{entity_id} has invalid temperature bounds")

    if not hass.services.has_service("ramses_cc", "set_zone_mode"):
        raise HomeAssistantError("RAMSES RF set_zone_mode is unavailable")

    return low, high


async def get_info(self: AdapterProbeHost, entity_id: str) -> DeviceChannels:
    return DeviceChannels(offset_write=False, valve_write=False)


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Control heating without RAMSES RF's default 25°C."""

    if hvac_mode == "heat":
        _LOGGER.debug("RAMSES %s: heat requested, awaiting setpoint", entity_id)
        return

    if hvac_mode == "off":
        state = self.hass.states.get(entity_id)
        if not state_is_live(state):
            raise WriteWithheldError(f"{entity_id} is unavailable")

        low, _ = validate_zone(self.hass, entity_id)

        await self.hass.services.async_call(
            "ramses_cc",
            "set_zone_mode",
            {"entity_id": entity_id, "mode": "permanent_override", "setpoint": low},
            blocking=True,
            context=self.context,
        )
        return

    raise HomeAssistantError(f"Unsupported RAMSES HVAC mode: {hvac_mode}")


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Apply calculated setpoint as permanent override."""

    state = self.hass.states.get(entity_id)
    if not state_is_live(state):
        raise WriteWithheldError(f"{entity_id} is unavailable")

    low, high = validate_zone(self.hass, entity_id)

    if not math.isfinite(temperature) or not low <= temperature <= high:
        raise HomeAssistantError(f"Invalid RAMSES setpoint: {temperature}")

    _LOGGER.debug("RAMSES %s: permanent override %.1f°C", entity_id, temperature)

    await self.hass.services.async_call(
        "ramses_cc",
        "set_zone_mode",
        {"entity_id": entity_id, "mode": "permanent_override", "setpoint": temperature},
        blocking=True,
        context=self.context,
    )
