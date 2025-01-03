import asyncio
import logging

from homeassistant.components.number.const import DOMAIN, SERVICE_SET_VALUE
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from ..utils.helpers import find_valve_entity
from .generic import (
    set_hvac_mode as generic_set_hvac_mode,
    set_temperature as generic_set_temperature,
)

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Get info from TRV."""
    support_valve = False
    valve = await find_valve_entity(self, entity_id)
    if valve is not None:
        support_valve = True
    return {"support_offset": False, "support_valve": support_valve}


async def init(self, entity_id):
    if self.real_trvs[entity_id]["valve_position_entity"] is None:
        self.real_trvs[entity_id]["valve_position_entity"] = await find_valve_entity(
            self, entity_id
        )
        _LOGGER.debug(
            "better_thermostat %s: uses valve position entity %s",
            self.name,
            self.real_trvs[entity_id]["valve_position_entity"],
        )
        # Wait for the entity to be available
        _ready = False
        while not _ready:
            if self.hass.states.get(
                self.real_trvs[entity_id]["valve_position_entity"]
            ).state in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
            ):
                _LOGGER.info(
                    "better_thermostat %s: waiting for TRV/climate entity with id '%s' to become fully available...",
                    self.name,
                    self.real_trvs[entity_id]["valve_position_entity"],
                )
                await asyncio.sleep(5)
                continue
            _ready = True
            return


async def get_current_offset(self, entity_id):
    """Get current offset."""
    return None


async def get_offset_steps(self, entity_id):
    """Get offset steps."""
    return None


async def get_min_offset(self, entity_id):
    """Get min offset."""
    return -6


async def get_max_offset(self, entity_id):
    """Get max offset."""
    return 6


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    return  # Not supported


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    _LOGGER.debug(
        f"better_thermostat {self.name}: TO TRV {entity_id} set_valve: {valve}"
    )
    value = min(round(valve * 100), 99)
    return await self.hass.services.async_call(
        DOMAIN,
        SERVICE_SET_VALUE,
        {
            "entity_id": self.real_trvs[entity_id]["valve_position_entity"],
            "value": value,
        },
        blocking=True,
        context=self.context,
    )
