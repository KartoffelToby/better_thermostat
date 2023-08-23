import logging

_LOGGER = logging.getLogger(__name__)


async def get_info(self, entity_id):
    """Get info from TRV."""
    return {"support_offset": False, "support_valve": False}


async def init(self, entity_id):
    return None


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
    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": temperature},
        blocking=True,
        context=self.context,
    )


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        context=self.context,
    )


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    return  # Not supported


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    return  # Not supported
