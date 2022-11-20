import logging

from ..utils.device_quirks import set_hvac_mode_quirk, set_temperature_quirk

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


async def set_temperature(self, entity_id, temperature):
    """Set new target temperature."""
    await set_temperature_quirk(self, entity_id, temperature)


async def set_hvac_mode(self, entity_id, hvac_mode):
    """Set new target hvac mode."""
    await set_hvac_mode_quirk(self, entity_id, hvac_mode)


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    return  # Not supported


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    return  # Not supported
