import asyncio
import logging

from homeassistant.components.number.const import SERVICE_SET_VALUE

from ..utils.helpers import find_local_calibration_entity, find_valve_entity
from .generic import (
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Get info from TRV."""
    support_offset = False
    support_valve = False
    offset = await find_local_calibration_entity(self, entity_id)
    if offset is not None:
        support_offset = True
    valve = await find_valve_entity(self, entity_id)
    if valve is not None:
        support_valve = True
    return {"support_offset": support_offset, "support_valve": support_valve}


async def init(self, entity_id):
    if (
        self.real_trvs[entity_id]["local_temperature_calibration_entity"] is None
        and self.real_trvs[entity_id]["calibration"] == 0
    ):
        self.real_trvs[entity_id][
            "local_temperature_calibration_entity"
        ] = await find_local_calibration_entity(self, entity_id)
        _LOGGER.debug(
            "better_thermostat %s: uses local calibration entity %s",
            self.name,
            self.real_trvs[entity_id]["local_temperature_calibration_entity"],
        )
        # Wait for the entity to be available
        _ready = True
        while _ready:
            if self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None):
                _LOGGER.info(
                    "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                    self.name,
                    self.real_trvs[entity_id]["local_temperature_calibration_entity"],
                )
                await asyncio.sleep(5)
                continue
            _ready = False
            return

        _has_preset = self.hass.states.get(entity_id).attributes.get(
            "preset_modes", None
        )
        if _has_preset is not None:
            await self.hass.services.async_call(
                "climate",
                "set_preset_mode",
                {"entity_id": entity_id, "preset_mode": "manual"},
                blocking=True,
                limit=None,
                context=self._context,
            )


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    await generic_set_hvac_mode(self, entity_id, hvac_mode)
    await asyncio.sleep(3)


async def get_current_offset(self, entity_id):
    """Get current offset."""
    return float(
        str(
            self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).state
        )
    )


async def get_offset_steps(self, entity_id):
    """Get offset steps."""
    return float(
        str(
            self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).attributes.get("step", 1)
        )
    )


async def get_min_offset(self, entity_id):
    """Get min offset."""
    # looks like z2m has a min max bug currently force to -10
    return -6.0
    return float(
        str(
            self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).attributes.get("min", -10)
        )
    )


async def get_max_offset(self, entity_id):
    """Get max offset."""
    # looks like z2m has a min max bug currently force to 10
    return 6.0
    return float(
        str(
            self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).attributes.get("max", 10)
        )
    )


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
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
        limit=None,
        context=self._context,
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


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    _LOGGER.debug(
        f"better_thermostat {self.name}: TO TRV {entity_id} set_valve: {valve}"
    )
    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {
            "entity_id": self.real_trvs[entity_id]["valve_position_entity"],
            "value": valve,
        },
        blocking=True,
        limit=None,
        context=self._context,
    )
