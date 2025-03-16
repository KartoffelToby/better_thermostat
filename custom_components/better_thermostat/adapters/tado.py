import logging
from .generic import (
    set_temperature as generic_set_temperature,
    set_hvac_mode as generic_set_hvac_mode,
)

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Get info from TRV."""
    return {"support_offset": True, "support_valve": False}


async def init(self, entity_id):
    return None


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


async def get_current_offset(self, entity_id):
    """Get current offset."""
    return float(
        str(self.hass.states.get(entity_id).attributes.get("offset_celsius", 0))
    )


async def get_offset_step(self, entity_id):
    """Get offset step."""
    return float(0.01)


async def get_min_offset(self, entity_id):
    """Get min offset."""
    return -10


async def get_max_offset(self, entity_id):
    """Get max offset."""
    return 10


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    if offset >= 10:
        offset = 10
    if offset <= -10:
        offset = -10
    await self.hass.services.async_call(
        "tado",
        "set_climate_temperature_offset",
        {"entity_id": entity_id, "offset": offset},
        blocking=True,
        context=self.context,
    )
    self.real_trvs[entity_id]["last_calibration"] = offset


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    return None
