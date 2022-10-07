import asyncio
from homeassistant.components.climate.const import HVAC_MODE_OFF
import logging

_LOGGER = logging.getLogger(__name__)


def get_info():
    """Get info from TRV."""
    return {"support_offset": False, "support_valve": False}


async def init(self):
    return None


async def set_temperature(self, temperature):
    """Set new target temperature."""
    _LOGGER.info(
        f"better_thermostat {self.name}: TO TRV set_temperature: {temperature}"
    )
    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": self.heater_entity_id, "temperature": temperature},
        blocking=True,
        limit=None,
        context=self._context,
    )


async def set_hvac_mode(self, hvac_mode):
    """Set new target hvac mode."""
    _LOGGER.info(f"better_thermostat {self.name}: TO TRV set_hvac_mode: {hvac_mode}")
    if hvac_mode == HVAC_MODE_OFF:
        await self.hass.services.async_call(
            "climate",
            "turn_off",
            {"entity_id": self.heater_entity_id},
            blocking=True,
            limit=None,
            context=self._context,
        )
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": self.heater_entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        limit=None,
        context=self._context,
    )
    await asyncio.sleep(2)


async def set_offset(self, offset):
    """Set new target offset."""
    return  # Not supported


async def set_valve(self, valve):
    """Set new target valve."""
    return  # Not supported
