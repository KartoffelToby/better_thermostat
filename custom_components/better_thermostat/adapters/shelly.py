"""Shelly adapter for TRV devices.

A strict superset of the generic adapter: it adds a valve channel for the
Shelly BLU TRV. Once the TRV's own thermostat is switched off, the Shelly
integration publishes the valve opening as a writable number entity, and
Better Thermostat writes its valve percentage there.

Only a BLU TRV valve is adopted. The Shelly TRV (Gen1) publishes a valve
position number as well, but that one sits next to a thermostat that keeps
running, and Better Thermostat has not been tried against it.
"""

from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er

from ..utils.const import CalibrationOutput
from ..utils.helpers import find_valve_entity
from .base import AdapterCapabilities
from .generic import (
    discover_calibration_entity,
    get_current_offset,
    get_info as generic_get_info,
    get_max_offset,
    get_min_offset,
    get_offset_step,
    set_hvac_mode,
    set_offset,
    set_temperature as generic_set_temperature,
)
from .types import AdapterHost, AdapterProbeHost
from .valve_entity import discover_valve_entity, write_valve_percent

__all__ = (
    "CAPABILITIES",
    "get_current_offset",
    "get_info",
    "get_max_offset",
    "get_min_offset",
    "get_offset_step",
    "init",
    "set_hvac_mode",
    "set_offset",
    "set_temperature",
    "set_valve",
)

_LOGGER = logging.getLogger(__name__)

# Shelly: the offset rides on a discovered calibration entity as on any
# climate entity, the valve on the BLU TRV's valve number entity.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=True)

# The Shelly integration keys a BLU TRV's entities on its ``blutrv:<id>``
# component, so the unique ID of its valve number carries that key.
_BLU_TRV_KEY = "-blutrv:"


def _is_blu_trv_entity(self: AdapterProbeHost, entity_id: str) -> bool:
    """Whether the entity belongs to a Shelly BLU TRV.

    Parameters
    ----------
    self : AdapterProbeHost
        Host providing Home Assistant access.
    entity_id : str
        Entity ID to look up in the entity registry.

    Returns
    -------
    bool
        True when the registry entry's unique ID names a BLU TRV component.
    """
    entry = er.async_get(self.hass).async_get(entity_id)
    if entry is None:
        return False
    return _BLU_TRV_KEY in str(entry.unique_id or "").lower()


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Report offset and valve capabilities of the TRV.

    The offset follows the generic adapter. The valve is offered once the
    TRV publishes a writable BLU TRV valve number, which the Shelly
    integration only does while the TRV's own thermostat is switched off.
    """
    info = await generic_get_info(self, entity_id)
    valve = await find_valve_entity(self, entity_id)
    valve_entity = valve.get("entity_id") if valve is not None else None
    support_valve = bool(
        valve is not None
        and valve_entity
        and valve.get("writable", False)
        and _is_blu_trv_entity(self, valve_entity)
    )
    return info | {"support_valve": support_valve}


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the Shelly adapter for a TRV entity.

    Adopts the valve number of a BLU TRV and the local calibration entity.
    A valve number of any other Shelly TRV is let go again, so no valve
    channel exists for it.
    """
    await discover_valve_entity(self, entity_id)
    trv = self.real_trvs[entity_id]
    if trv.valve_position_entity and not _is_blu_trv_entity(
        self, trv.valve_position_entity
    ):
        _LOGGER.debug(
            "better_thermostat %s: valve entity %s of %s is no BLU TRV valve, "
            "not adopted",
            self.device_name,
            trv.valve_position_entity,
            entity_id,
        )
        trv.valve_position_entity = None
        trv.valve_position_writable = None
    await discover_calibration_entity(self, entity_id)


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Set a new target temperature, unless the TRV has none to set.

    A BLU TRV under direct valve control has its own thermostat switched
    off and reports no target temperature. A setpoint sent then takes the
    slow gateway path for nothing, so it is left out.
    """
    trv = self.real_trvs.get(entity_id)
    state = self.hass.states.get(entity_id)
    if (
        trv is not None
        and trv.advanced.get("calibration") == CalibrationOutput.DIRECT_VALVE_BASED
        and trv.valve_position_entity
        and trv.valve_position_writable is True
        and state is not None
        and state.attributes.get("temperature") is None
    ):
        _LOGGER.debug(
            "better_thermostat %s: %s runs on its valve and reports no target "
            "temperature, skip set_temperature(%s)",
            self.device_name,
            entity_id,
            temperature,
        )
        return
    await generic_set_temperature(self, entity_id, temperature)


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Write a valve position (0-100 %) to the BLU TRV's valve number entity."""
    await write_valve_percent(self, entity_id, valve)
