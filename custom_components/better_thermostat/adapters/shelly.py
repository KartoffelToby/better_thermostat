"""Shelly thermostat adapter with exclusive control of valve-position numbers."""

from __future__ import annotations

import asyncio
import logging
import math

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

from . import generic

_LOGGER = logging.getLogger(__name__)


def _normalize_valve_value(percent, attributes) -> float:
    """Map a percentage to the Shelly number's bounds and step grid."""
    minimum = float(attributes.get("min", 0))
    maximum = float(attributes.get("max", 100))
    step = float(attributes.get("step", 1))
    percent = float(percent)
    if not all(math.isfinite(x) for x in (minimum, maximum, step, percent)):
        raise ValueError("Non-finite valve request or bounds")
    if maximum < minimum:
        raise ValueError("Inverted valve bounds")
    percent = max(0.0, min(100.0, percent))
    value = minimum + percent / 100.0 * (maximum - minimum)
    if step > 0:
        value = minimum + round((value - minimum) / step) * step
    return max(minimum, min(maximum, value))


async def _find_valve(self, entity_id):
    """Find an enabled Shelly number on this TRV, never another gateway child."""
    registry = er.async_get(self.hass)
    climate = registry.async_get(entity_id)
    if climate is None or climate.platform != "shelly" or not climate.device_id:
        return None

    for candidate in er.async_entries_for_device(registry, climate.device_id):
        if (
            candidate.platform == "shelly"
            and candidate.domain == "number"
            and candidate.disabled_by is None
            and candidate.config_entry_id == climate.config_entry_id
            and candidate.translation_key in ("valve_position", "valve")
        ):
            return {"entity_id": candidate.entity_id, "writable": True}
    return None


async def get_info(self, entity_id):
    """Advertise valve support through registry metadata and exclusive policy."""
    info = await generic.get_info(self, entity_id)
    valve = await _find_valve(self, entity_id)
    info.update(support_valve=valve is not None, direct_valve_exclusive=True)
    return info


async def _discover(self, entity_id):
    """Refresh the actuator, including after removal, rename or re-enabling."""
    trv = self.real_trvs[entity_id]
    valve = await _find_valve(self, entity_id)
    trv.valve_position_entity = valve["entity_id"] if valve else None
    trv.valve_position_writable = valve is not None
    return trv.valve_position_entity


async def init(self, entity_id):
    """Establish ownership before discovery can fail or an entity can disappear."""
    trv = self.real_trvs[entity_id]
    trv.direct_valve_exclusive = True
    await _discover(self, entity_id)
    _LOGGER.debug(
        "Shelly %s: valve=%s, exclusive direct control=%s",
        entity_id,
        trv.valve_position_entity,
        trv.exclusive_direct_valve,
    )
    if not trv.exclusive_direct_valve:
        await generic.init(self, entity_id)


async def set_valve(self, entity_id, valve):
    """Write a position through HA; return False unless the write succeeded."""
    try:
        # Only this Shelly actuator waits for its previous write, including
        # a control command already in flight when maintenance starts.
        trv = self.real_trvs[entity_id]
        lock = trv.extra.setdefault("shelly_valve_lock", asyncio.Lock())
        async with lock:
            valve_id = await _discover(self, entity_id)
            if valve_id is None:
                return False
            state = self.hass.states.get(valve_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return False
            value = _normalize_valve_value(valve, state.attributes)
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": valve_id, "value": value},
                blocking=True,
                context=self.context,
            )
    except Exception:
        _LOGGER.debug("Shelly %s: valve write failed", entity_id, exc_info=True)
        return False
    _LOGGER.debug("Shelly %s: valve %s%% -> %s", entity_id, valve, value)
    return True


async def get_current_offset(self, entity_id):
    """Get current thermostat offset."""
    return await generic.get_current_offset(self, entity_id)


async def get_offset_step(self, entity_id):
    """Get thermostat offset step."""
    return await generic.get_offset_step(self, entity_id)


async def get_min_offset(self, entity_id):
    """Get minimum thermostat offset."""
    return await generic.get_min_offset(self, entity_id)


async def get_max_offset(self, entity_id):
    """Get maximum thermostat offset."""
    return await generic.get_max_offset(self, entity_id)


async def set_temperature(self, entity_id, temperature):
    """Set the target in ordinary thermostat mode."""
    return await generic.set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set the mode in ordinary thermostat mode."""
    return await generic.set_hvac_mode(self, entity_id, hvac_mode)


async def set_offset(self, entity_id, offset):
    """Set the offset in ordinary thermostat mode."""
    return await generic.set_offset(self, entity_id, offset)
