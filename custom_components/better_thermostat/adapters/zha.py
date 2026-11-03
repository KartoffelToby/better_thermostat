"""ZHA adapter for TRV devices.

ZHA serves a TRV through a climate entity like any other integration, so
the setpoint, the mode and the offset follow the generic adapter. What a
ZHA quirk adds on top is a valve opening published as a number entity of
its own; this adapter offers that number as the valve channel once it
accepts writes. A TRV whose quirk publishes the valve only as a sensor
keeps the channels of the generic adapter.
"""

from __future__ import annotations

from .base import AdapterCapabilities
from .generic import (
    discover_calibration_entity,
    get_calibration_offset,
    get_calibration_offset_step,
    get_max_calibration_offset,
    get_min_calibration_offset,
    set_calibration_offset,
    set_hvac_mode,
    set_temperature,
)
from .types import AdapterHost
from .valve_entity import (
    discover_valve_entity,
    find_entity_channels as get_info,
    write_valve_percent,
)

__all__ = (
    "CAPABILITIES",
    "get_calibration_offset",
    "get_info",
    "get_max_calibration_offset",
    "get_min_calibration_offset",
    "get_calibration_offset_step",
    "init",
    "set_hvac_mode",
    "set_calibration_offset",
    "set_temperature",
    "set_valve",
)

# ZHA: offset and valve position via discovered number entities.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=True)


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the ZHA adapter for a TRV entity.

    Adopts the valve number entity and the local calibration entity.
    """
    await discover_valve_entity(self, entity_id)
    await discover_calibration_entity(self, entity_id)


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Write a valve position (0-100 %) to the TRV's valve number entity."""
    await write_valve_percent(self, entity_id, valve)
