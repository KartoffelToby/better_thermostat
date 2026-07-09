"""Z-Wave JS adapter for TRV devices.

Implements Z-Wave JS specific behaviour for TRV devices used by Better
Thermostat. The adapter is a strict superset of the generic adapter: when a
device exposes neither a writable valve helper nor a local calibration entity
it behaves exactly like ``generic``. Direct valve control is only offered when
the device actually exposes a writable valve ``number`` entity (e.g. the
Eurotronic Spirit Z / Aeotec ZWA021 family under its manufacturer-specific
mode).
"""

import asyncio
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import find_local_calibration_entity, find_valve_entity
from .base import wait_for_calibration_entity_or_timeout
from .generic import (
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Report offset and valve capabilities of the TRV.

    Capabilities are derived from the entities the device actually exposes, so
    a Z-Wave JS TRV without a writable valve helper falls back to the same
    (offset-only or plain) behaviour as the generic adapter.
    """
    support_offset = False
    support_valve = False
    offset = await find_local_calibration_entity(self, entity_id)
    if offset is not None:
        support_offset = True
    valve = await find_valve_entity(self, entity_id)
    if valve is not None and valve.get("entity_id"):
        support_valve = bool(valve.get("writable", False))
    return {"support_offset": support_offset, "support_valve": support_valve}


async def init(self, entity_id):
    """Initialize the Z-Wave JS adapter for a TRV entity.

    Discovers the valve position entity (when writable) and the local
    calibration entity early, mirroring the generic initialization for the
    calibration path.
    """
    # Discover a writable valve helper early so the reconciler can use it.
    try:
        valve = await find_valve_entity(self, entity_id)
        if valve is not None:
            self.real_trvs[entity_id]["valve_position_entity"] = valve.get("entity_id")
            self.real_trvs[entity_id]["valve_position_writable"] = bool(
                valve.get("writable", False)
            )
    except Exception:
        _LOGGER.debug(
            "better_thermostat %s: valve discovery failed for %s",
            self.device_name,
            entity_id,
        )

    if (
        self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None
        and self.real_trvs[entity_id]["calibration"] != 1
    ):
        self.real_trvs[entity_id][
            "local_temperature_calibration_entity"
        ] = await find_local_calibration_entity(self, entity_id)
        _LOGGER.debug(
            "better_thermostat %s: uses local calibration entity %s",
            self.device_name,
            self.real_trvs[entity_id]["local_temperature_calibration_entity"],
        )
        if (
            self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            is not None
        ):
            await wait_for_calibration_entity_or_timeout(
                self,
                entity_id,
                self.real_trvs[entity_id]["local_temperature_calibration_entity"],
            )
        else:
            _LOGGER.warning(
                "better_thermostat %s: no local calibration entity found for '%s', skipping calibration init",
                self.device_name,
                entity_id,
            )


async def get_current_offset(self, entity_id):
    """Get current offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None:
        return 0.0
    state = self.hass.states.get(
        self.real_trvs[entity_id]["local_temperature_calibration_entity"]
    )
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


async def get_offset_step(self, entity_id):
    """Get offset step."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None:
        return None
    state = self.hass.states.get(
        self.real_trvs[entity_id]["local_temperature_calibration_entity"]
    )
    if state is None:
        return None
    return float(str(state.attributes.get("step", 1)))


async def get_min_offset(self, entity_id):
    """Get min offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None:
        return -6.0
    state = self.hass.states.get(
        self.real_trvs[entity_id]["local_temperature_calibration_entity"]
    )
    if state is None:
        return -6.0
    return float(str(state.attributes.get("min", -10)))


async def get_max_offset(self, entity_id):
    """Get max offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None:
        return 6.0
    state = self.hass.states.get(
        self.real_trvs[entity_id]["local_temperature_calibration_entity"]
    )
    if state is None:
        return 6.0
    return float(str(state.attributes.get("max", 10)))


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    if self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None:
        return  # Not supported

    max_calibration = await get_max_offset(self, entity_id)
    min_calibration = await get_min_offset(self, entity_id)

    offset = min(max_calibration, offset)
    offset = max(min_calibration, offset)

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
        return await generic_set_hvac_mode(
            self, entity_id, self.real_trvs[entity_id]["last_hvac_mode"]
        )
    return offset


async def set_valve(self, entity_id, valve):
    """Write a valve position (0-100 %) to the device's valve number entity."""
    _LOGGER.debug(
        "better_thermostat %s: TO TRV %s set_valve: %s",
        self.device_name,
        entity_id,
        valve,
    )
    if self.real_trvs.get(entity_id, {}).get("valve_position_writable") is False:
        _LOGGER.debug(
            "better_thermostat %s: valve entity for %s is read-only, skip adapter write",
            self.device_name,
            entity_id,
        )
        return

    valve_entity_id = self.real_trvs[entity_id]["valve_position_entity"]
    if valve_entity_id is None:
        return

    # Scale the 0-100 % request onto the number entity's own min/max/step.
    valve_entity = self.hass.states.get(valve_entity_id)
    if valve_entity is not None:
        min_valve = float(str(valve_entity.attributes.get("min", 0)))
        max_valve = float(str(valve_entity.attributes.get("max", 100)))
        valve = min_valve + (valve / 100.0) * (max_valve - min_valve)
        step = float(str(valve_entity.attributes.get("step", 1)))
        if step > 0:
            valve = round(valve / step) * step

    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": valve_entity_id, "value": valve},
        blocking=True,
        context=self.context,
    )
