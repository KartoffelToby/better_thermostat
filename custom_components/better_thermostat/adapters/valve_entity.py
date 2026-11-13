"""The valve channel a device exposes as a number entity of its own.

Zigbee2MQTT, ZHA and Z-Wave JS publish the valve opening as a separate
number entity next to the climate entity, so these ecosystems reach the
valve the same way: a lookup stores the entity and whether it accepts
writes, and a write scales Better Thermostat's percentage onto the grid
that entity itself publishes.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import State

from ..utils.helpers import find_local_calibration_entity, find_valve_entity
from .base import DeviceChannels
from .types import AdapterHost, AdapterProbeHost

_LOGGER = logging.getLogger(__name__)


class ValveEntityUnreachableError(RuntimeError):
    """The valve entity has no state a valve position could be written to."""


class ValveEntityRange(NamedTuple):
    """The grid a valve number entity publishes for the values it takes.

    Attributes
    ----------
    minimum : float
        The entity's ``min``, the value of a fully closed valve
    maximum : float
        The entity's ``max``, the value of a fully open valve
    step : float
        The entity's ``step``; zero or less means no grid
    """

    minimum: float
    maximum: float
    step: float


def valve_entity_range(state: State) -> ValveEntityRange:
    """Read the grid a valve number entity publishes.

    An entity that publishes no ``min``, ``max`` or ``step`` is taken to
    span 0 to 100 in steps of 1, the grid of a plain percentage.

    Parameters
    ----------
    state : State
        The valve number entity's current state.

    Returns
    -------
    ValveEntityRange
        The entity's minimum, maximum and step.

    Raises
    ------
    ValueError
        When one of the three attributes is not a number.
    """
    return ValveEntityRange(
        float(str(state.attributes.get("min", 0))),
        float(str(state.attributes.get("max", 100))),
        float(str(state.attributes.get("step", 1))),
    )


def valve_entity_value(valve_range: ValveEntityRange, valve_percent: float) -> float:
    """Scale an opening degree onto a valve number entity's own grid.

    The percentage is clamped to 0-100 and mapped linearly onto the range.
    The result is quantized onto the step grid, which starts at the minimum
    rather than at zero, and clamped again, so neither a rounding step nor
    an out-of-range input leaves the entity's bounds.

    Parameters
    ----------
    valve_range : ValveEntityRange
        The grid the entity publishes.
    valve_percent : float
        Opening degree, 0-100.

    Returns
    -------
    float
        The value the entity takes for that opening.
    """
    minimum, maximum, step = valve_range
    percent = max(0.0, min(100.0, valve_percent))
    value = minimum + (percent / 100.0) * (maximum - minimum)
    if step > 0:
        value = minimum + round((value - minimum) / step) * step
    return max(minimum, min(maximum, value))


async def find_entity_channels(
    self: AdapterProbeHost, entity_id: str
) -> DeviceChannels:
    """Report the channels a TRV offers through entities of its own.

    The offset is offered once a local calibration entity exists, the
    valve once a valve entity exists that accepts writes. A valve entity
    that only reports the opening is no valve channel.

    Parameters
    ----------
    self : AdapterProbeHost
        Host providing Home Assistant access and the name to log under.
    entity_id : str
        Entity ID of the TRV to look up.

    Returns
    -------
    DeviceChannels
        The offset and valve channels the discovered entities provide.
    """
    calibration_entity_id = await find_local_calibration_entity(self, entity_id)
    valve = await find_valve_entity(self, entity_id)
    return DeviceChannels(
        offset_write=calibration_entity_id is not None,
        valve_write=bool(
            valve is not None and valve.get("entity_id") and valve.get("writable")
        ),
    )


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
        valve = await find_valve_entity(
            self, entity_id, trv=self.real_trvs.get(entity_id)
        )
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
    trv.valve_position_writable = valve.get("writable", False)


async def write_valve_percent(
    self: AdapterHost, entity_id: str, valve_percent: float
) -> None:
    """Write an opening degree to the TRV's valve number entity.

    The request is scaled onto the grid the entity publishes, as
    :func:`valve_entity_value` describes.

    Nothing is written for a TRV whose valve entity is known to be
    read-only or was never discovered; neither one is a valve channel.

    A valve entity that reports no state, or reports ``unavailable``, takes
    no write either: without a state there are no bounds to scale onto, and
    Home Assistant drops a service call to an unavailable entity without
    saying so. Both raise, so the caller neither records the position as
    sent nor takes a later write of the same position as already done.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to.
    valve_percent : float
        Opening degree Better Thermostat asks for, 0-100.

    Raises
    ------
    ValveEntityUnreachableError
        When the valve entity reports no state or reports ``unavailable``.
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
    if valve_entity is None or valve_entity.state == STATE_UNAVAILABLE:
        raise ValveEntityUnreachableError(
            f"valve entity {valve_entity_id} for {entity_id} reports "
            f"{valve_entity.state if valve_entity is not None else 'no state'}, "
            "so it cannot take a valve position"
        )

    value = valve_entity_value(valve_entity_range(valve_entity), valve_percent)

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": valve_entity_id, "value": value},
        blocking=True,
        context=self.context,
    )
