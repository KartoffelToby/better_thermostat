"""RAMSES RF adapter for Honeywell Evohome heating zones."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from ..utils.const import DOMAIN
from ..utils.helpers import state_is_live
from ..utils.retry import WriteWithheldError
from .base import AdapterCapabilities, DeviceChannels
from .generic import (
    get_calibration_offset,
    get_calibration_offset_step,
    get_max_calibration_offset,
    get_min_calibration_offset,
    init,
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
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ramses_invalid_zone",
            translation_placeholders={"entity": entity_id},
        )

    config = state.attributes.get("config")
    if not isinstance(config, Mapping):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ramses_missing_zone_config",
            translation_placeholders={"entity": entity_id},
        )

    try:
        low = float(config["min_temp"])
        high = float(config["max_temp"])
    except (KeyError, TypeError, ValueError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ramses_invalid_zone_bounds",
            translation_placeholders={"entity": entity_id},
        ) from err

    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ramses_invalid_zone_bounds",
            translation_placeholders={"entity": entity_id},
        )

    if not hass.services.has_service("ramses_cc", "set_zone_mode"):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="ramses_service_unavailable"
        )

    return low, high


async def get_info(self: AdapterProbeHost, entity_id: str) -> DeviceChannels:
    """Get info from TRV."""
    return DeviceChannels(offset_write=False, valve_write=False)


async def set_zone_override(
    self: AdapterHost, entity_id: str, hvac_mode: str, temperature: float | None
) -> None:
    """Set zone temperature and handle HVAC states.

    The default behavior of RAMSES is to set the target temperature to 25
    degrees Celsius when the 'heat' HVAC mode is selected. That leads to the
    conflict between Better Thermostat and RAMSES. While the first one is
    trying to set the desired temperature, RAMSES keeps switching it to
    the default

    The workaround is to use the 'set_zone_mode' action provided by RAMSES,
    which allows setting permament overrides for the zone.
    """

    state = self.hass.states.get(entity_id)
    if not state_is_live(state):
        raise WriteWithheldError(f"{entity_id} is unavailable")

    low, high = validate_zone(self.hass, entity_id)

    if hvac_mode == "off":
        temperature = low
    elif hvac_mode != "heat":
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ramses_unsupported_hvac_mode",
            translation_placeholders={"entity": entity_id, "mode": hvac_mode},
        )

    if (
        temperature is None
        or not math.isfinite(temperature)
        or not low <= temperature <= high
    ):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="ramses_invalid_setpoint",
            translation_placeholders={
                "entity": entity_id,
                "temperature": str(temperature),
            },
        )

    _LOGGER.debug(
        "RAMSES %s: HVAC mode '%s' with permanent override %.1f°C",
        entity_id,
        hvac_mode,
        temperature,
    )

    await self.hass.services.async_call(
        "ramses_cc",
        "set_zone_mode",
        {"entity_id": entity_id, "mode": "permanent_override", "setpoint": temperature},
        blocking=True,
        context=self.context,
    )


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Control heating without RAMSES RF's default 25°C."""

    # Temperature is applied by set_temperature().
    if hvac_mode == "heat":
        return

    await set_zone_override(self, entity_id, hvac_mode, None)


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Apply calculated setpoint as permanent override."""

    await set_zone_override(self, entity_id, "heat", temperature)


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Evohome zones expose no writable valve state."""
    return None


async def set_calibration_offset(
    self: AdapterHost, entity_id: str, calibration_offset: float
) -> bool:
    """Evohome zones expose no writable calibration offset."""
    return False
