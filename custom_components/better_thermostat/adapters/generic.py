"""Generic adapter helpers used by multiple TRV integrations.

This module implements the generic, default behaviour for TRV adapters
used by Better Thermostat when a device-specific adapter does not exist.
"""

import asyncio
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE
from ..utils.helpers import find_local_calibration_entity
from .base import wait_for_calibration_entity_or_timeout
from ..utils.helpers import normalize_hvac_mode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Get info from TRV."""
    support_offset = False

    offset = await find_local_calibration_entity(self, entity_id)
    if offset is not None:
        support_offset = True
    return {"support_offset": support_offset, "support_valve": False}


async def init(self, entity_id):
    """Initialize generic adapter for an entity.

    Finds and registers a local calibration entity (if configured) and waits
    for it to appear before returning. Returns None after initialization.
    """
    if (
        self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None
        and self.real_trvs[entity_id]["calibration"] != 1
    ):
        self.real_trvs[entity_id]["local_temperature_calibration_entity"] = (
            await find_local_calibration_entity(self, entity_id)
        )
        _LOGGER.debug(
            "better_thermostat %s: uses local calibration entity %s",
            self.device_name,
            self.real_trvs[entity_id]["local_temperature_calibration_entity"],
        )
        await wait_for_calibration_entity_or_timeout(
            self,
            entity_id,
            self.real_trvs[entity_id]["local_temperature_calibration_entity"],
        )

    return None


async def get_current_offset(self, entity_id):
    """Get current offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is not None:
        state = self.hass.states.get(
            self.real_trvs[entity_id]["local_temperature_calibration_entity"]
        )
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return 0.0
        try:
            return float(str(state.state))
        except (ValueError, TypeError):
            _LOGGER.warning(
                "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
                self.device_name,
                state.state,
            )
            return 0.0
    else:
        return 0.0


async def get_offset_step(self, entity_id):
    """Get offset step."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is not None:
        state = self.hass.states.get(
            self.real_trvs[entity_id]["local_temperature_calibration_entity"]
        )
        if state is None:
            return None
        return float(str(state.attributes.get("step", 1)))
    else:
        return None


async def get_min_offset(self, entity_id):
    """Get min offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is not None:
        state = self.hass.states.get(
            self.real_trvs[entity_id]["local_temperature_calibration_entity"]
        )
        if state is None:
            return -6.0
        return float(str(state.attributes.get("min", -10)))
    else:
        return -6


async def get_max_offset(self, entity_id):
    """Get max offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is not None:
        state = self.hass.states.get(
            self.real_trvs[entity_id]["local_temperature_calibration_entity"]
        )
        if state is None:
            return 6.0
        return float(str(state.attributes.get("max", 10)))
    else:
        return 6


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": temperature},
        blocking=True,
        context=self.context,
    )


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""

    hvac_mode_norm = normalize_hvac_mode(hvac_mode)
    _LOGGER.debug(
        "better_thermostat %s: set_hvac_mode %s -> %s",
        self.device_name,
        hvac_mode,
        hvac_mode_norm,
    )
    try:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode_norm},
            blocking=True,
            context=self.context,
        )
    except TypeError:
        _LOGGER.debug(
            "TypeError in set_hvac_mode (entity=%s, hvac_mode=%s)",
            entity_id,
            hvac_mode_norm,
        )
    except Exception as exc:
        _LOGGER.exception(
            "better_thermostat %s: Exception in set_hvac_mode for %s with %s: %s",
            self.device_name,
            entity_id,
            hvac_mode_norm,
            exc,
        )


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is not None:
        max_calibration = await get_max_offset(self, entity_id)
        min_calibration = await get_min_offset(self, entity_id)

        if offset >= max_calibration:
            offset = max_calibration
        if offset <= min_calibration:
            offset = min_calibration

        await self.hass.services.async_call(
            "number",
            SERVICE_SET_VALUE,
            {
                "entity_id": self.real_trvs[entity_id][
                    "local_temperature_calibration_entity"
                ],
                "value": offset,
            },
            blocking=True,
            context=self.context,
        )
        self.real_trvs[entity_id]["last_calibration"] = offset
        if (
            self.real_trvs[entity_id]["last_hvac_mode"] is not None
            and self.real_trvs[entity_id]["last_hvac_mode"] != "off"
        ):
            await asyncio.sleep(3)
            await set_hvac_mode(
                self, entity_id, self.real_trvs[entity_id]["last_hvac_mode"]
            )

        return offset
    else:
        return  # Not supported


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    return  # Not supported
