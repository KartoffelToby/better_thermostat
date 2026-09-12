"""The valve channel a device exposes as a number entity of its own.

Zigbee2MQTT and Z-Wave JS both publish the valve opening as a separate
number entity next to the climate entity, so both ecosystems reach the
valve the same way: a lookup stores the entity and whether it accepts
writes, and a write scales Better Thermostat's percentage onto the grid
that entity itself publishes.
"""

from __future__ import annotations

import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE

from ..utils.helpers import find_valve_entity
from .types import AdapterHost

_LOGGER = logging.getLogger(__name__)


async def discover_valve_entity(self: AdapterHost, entity_id: str) -> None:
    """Adopt the TRV's valve number entity and its writability.

    The lookup walks the entity and device registries, so it is best
    effort: one that raises leaves the TRV without a valve entity and
    reports why, and initialization carries on with the climate channel
    alone.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to run the lookup for.
    """
    try:
        valve = await find_valve_entity(self, entity_id)
    except Exception:
        _LOGGER.exception(
            "better_thermostat %s: valve entity discovery for %s failed",
            self.device_name,
            entity_id,
        )
        return

    if valve is None:
        return

    trv = self.real_trvs[entity_id]
    trv.valve_position_entity = valve.get("entity_id")
    trv.valve_position_writable = bool(valve.get("writable", False))


async def write_valve_percent(
    self: AdapterHost, entity_id: str, valve_percent: float
) -> None:
    """Write an opening degree to the TRV's valve number entity.

    The request is scaled from 0-100 % onto the entity's own min/max/step,
    clamping both the incoming percentage and the quantized result so a
    rounding step or an out-of-range input never leaves the entity's
    bounds. The step grid starts at the entity's minimum rather than at
    zero, so a non-zero minimum still yields a value the entity itself
    offers.

    Nothing is written for a TRV whose valve entity is known to be
    read-only, was never discovered, or reports no state: without a state
    there are no bounds to scale onto.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to.
    valve_percent : float
        Opening degree Better Thermostat asks for, 0-100.
    """
    _LOGGER.debug(
        "better_thermostat %s: TO TRV %s set_valve: %s",
        self.device_name,
        entity_id,
        valve_percent,
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
    pct = max(0.0, min(100.0, valve_percent))
    value = min_valve + (pct / 100.0) * (max_valve - min_valve)
    step = float(str(valve_entity.attributes.get("step", 1)))
    if step > 0:
        value = min_valve + round((value - min_valve) / step) * step
    value = max(min_valve, min(max_valve, value))

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": valve_entity_id, "value": value},
        blocking=True,
        context=self.context,
    )
