"""Z-Wave JS adapter for TRV devices.

Implements Z-Wave JS specific behaviour for TRV devices used by Better
Thermostat. The adapter is a strict superset of the generic adapter: when a
device exposes neither a writable valve helper nor a local calibration entity
it behaves exactly like ``generic``. Valve control is offered for devices that
expose a writable valve ``number`` entity. The Eurotronic Spirit Z / Aeotec
ZWA021 family drives its valve through the Multilevel Switch command class
instead; that channel belongs to its model quirk, which offers it whichever
adapter serves the device.
"""

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

# Z-Wave JS: both channels ride on a discovered number entity, so each is
# offered only for devices that expose one.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=True)


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the Z-Wave JS adapter for a TRV entity.

    Adopts the valve position entity, so the reconciler can use it, and
    the local calibration entity.
    """
    await discover_valve_entity(self, entity_id)
    await discover_calibration_entity(self, entity_id)


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Write a valve position (0-100 %) to the device's valve number entity."""
    await write_valve_percent(self, entity_id, valve)
