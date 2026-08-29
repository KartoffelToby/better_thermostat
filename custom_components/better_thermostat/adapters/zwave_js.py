"""Z-Wave JS adapter for TRV devices.

Implements Z-Wave JS specific behaviour for TRV devices used by Better
Thermostat. The adapter is a strict superset of the generic adapter: when a
device exposes neither a writable valve helper nor a local calibration entity
it behaves exactly like ``generic``. Valve control is offered for devices that
expose a writable valve ``number`` entity, and additionally for models whose
valve is driven through a model quirk (the Eurotronic Spirit Z / Aeotec ZWA021
family, which control the valve via the Multilevel Switch command class while
in their manufacturer-specific mode).
"""

import asyncio
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import (
    find_local_calibration_entity,
    find_valve_entity,
    get_device_model,
)
from .base import AdapterCapabilities
from .generic import (
    discover_calibration_entity,
    get_max_offset as generic_get_max_offset,
    get_min_offset as generic_get_min_offset,
    get_offset_step as generic_get_offset_step,
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)
from .types import AdapterHost, AdapterProbeHost
from .valve_entity import discover_valve_entity, write_valve_percent

_LOGGER = logging.getLogger(__name__)

# Z-Wave JS: both channels ride on a discovered number entity, so each is
# offered only for devices that expose one.
CAPABILITIES = AdapterCapabilities(offset_write=True, valve_write=True)

# Models whose valve is driven through a model quirk (Multilevel Switch command
# class while in manufacturer-specific mode) rather than a writable number
# helper. For these, valve support is reported even though no valve number
# entity is exposed, so the config flow offers direct valve control.
_QUIRK_VALVE_MODELS = {"ZWA021"}


async def get_info(self: AdapterProbeHost, entity_id: str) -> dict[str, bool]:
    """Report offset and valve capabilities of the TRV.

    Capabilities are derived from the entities the device actually exposes, so
    a Z-Wave JS TRV without a writable valve helper falls back to the same
    (offset-only or plain) behaviour as the generic adapter. Devices whose valve
    is controlled through a model quirk are reported as valve-capable regardless.
    """
    support_offset = False
    support_valve = False
    offset = await find_local_calibration_entity(self, entity_id)
    if offset is not None:
        support_offset = True
    valve = await find_valve_entity(self, entity_id)
    if valve is not None and valve.get("entity_id"):
        support_valve = bool(valve.get("writable", False))
    if not support_valve:
        try:
            model = await get_device_model(self, entity_id)
        except Exception:
            model = ""
        if model in _QUIRK_VALVE_MODELS:
            support_valve = True
    return {"support_offset": support_offset, "support_valve": support_valve}


async def init(self: AdapterHost, entity_id: str) -> None:
    """Initialize the Z-Wave JS adapter for a TRV entity.

    Adopts the valve position entity, so the reconciler can use it, and
    the local calibration entity.
    """
    await discover_valve_entity(self, entity_id)
    await discover_calibration_entity(self, entity_id)


async def get_current_offset(self: AdapterHost, entity_id: str) -> float:
    """Get current offset."""
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is None:
        return 0.0
    state = self.hass.states.get(calibration_entity)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return 0.0
    try:
        return float(str(state.state))
    except ValueError, TypeError:
        _LOGGER.warning(
            "better_thermostat %s: Could not convert calibration offset '%s' to float, using 0",
            self.device_name,
            state.state,
        )
        return 0.0


async def get_offset_step(self: AdapterHost, entity_id: str) -> float:
    """Get offset step."""
    return await generic_get_offset_step(self, entity_id)


async def get_min_offset(self: AdapterHost, entity_id: str) -> float:
    """Get min offset."""
    return await generic_get_min_offset(self, entity_id)


async def get_max_offset(self: AdapterHost, entity_id: str) -> float:
    """Get max offset."""
    return await generic_get_max_offset(self, entity_id)


async def set_temperature(
    self: AdapterHost, entity_id: str, temperature: float
) -> None:
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self: AdapterHost, entity_id: str, hvac_mode: str) -> None:
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


async def set_offset(self: AdapterHost, entity_id: str, offset: float) -> bool:
    """Write a calibration offset to the discovered calibration entity.

    Parameters
    ----------
    self : AdapterHost
        Host providing Home Assistant access and the per-TRV records.
    entity_id : str
        Entity ID of the TRV to write to
    offset : float
        Calibration offset in Kelvin, clamped to the device's declared range

    Returns
    -------
    bool
        True once the write went out, False when no calibration entity was
        discovered for this TRV and there is nothing to write to.
    """
    calibration_entity = self.real_trvs[entity_id].local_temperature_calibration_entity
    if calibration_entity is None:
        return False

    max_calibration = await get_max_offset(self, entity_id)
    min_calibration = await get_min_offset(self, entity_id)

    offset = min(max_calibration, offset)
    offset = max(min_calibration, offset)

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": calibration_entity, "value": offset},
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id].last_calibration = offset
    last_hvac_mode = self.real_trvs[entity_id].last_hvac_mode
    if last_hvac_mode is not None and last_hvac_mode != "off":
        await asyncio.sleep(3)
        await generic_set_hvac_mode(self, entity_id, last_hvac_mode)
    return True


async def set_valve(self: AdapterHost, entity_id: str, valve: float) -> None:
    """Write a valve position (0-100 %) to the device's valve number entity."""
    await write_valve_percent(self, entity_id, valve)
