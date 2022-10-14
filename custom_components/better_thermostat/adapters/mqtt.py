from homeassistant.components.number.const import SERVICE_SET_VALUE
from datetime import datetime
import logging
from .generic import (
    set_temperature as generic_set_temperature,
    set_hvac_mode as generic_set_hvac_mode,
)
from ..utils.helpers import find_local_calibration_entity, find_valve_entity

_LOGGER = logging.getLogger(__name__)


async def get_info(self):
    """Get info from TRV."""
    support_offset = False
    support_valve = False
    offset = await find_local_calibration_entity(self)
    if offset is not None:
        support_offset = True
    valve = await find_valve_entity(self)
    if valve is not None:
        support_valve = True
    return {"support_offset": support_offset, "support_valve": support_valve}


async def init(self):
    if self.local_temperature_calibration_entity is None and self.calibration_type == 0:
        self.local_temperature_calibration_entity = await find_local_calibration_entity(
            self
        )
        _LOGGER.debug(
            "better_thermostat %s: uses local calibration entity %s",
            self.name,
            self.local_temperature_calibration_entity,
        )
        await set_offset(self, 0)


async def set_temperature(self, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, temperature)


async def set_hvac_mode(self, hvac_mode):
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, hvac_mode)


async def get_current_offset(self):
    """Get current offset."""
    return float(
        str(self.hass.states.get(self.local_temperature_calibration_entity).state)
    )


async def get_offset_steps(self):
    """Get offset steps."""
    return float(
        str(
            self.hass.states.get(
                self.local_temperature_calibration_entity
            ).attributes.get("step", 1)
        )
    )


async def set_offset(self, offset):
    """Set new target offset."""
    old = float(
        str(self.hass.states.get(self.local_temperature_calibration_entity).state)
    )
    max_calibration = float(
        str(
            self.hass.states.get(
                self.local_temperature_calibration_entity
            ).attributes.get("max", 127)
        )
    )
    min_calibration = float(
        str(
            self.hass.states.get(
                self.local_temperature_calibration_entity
            ).attributes.get("min", -128)
        )
    )

    if offset >= max_calibration:
        offset = max_calibration
    if offset <= min_calibration:
        offset = min_calibration

    _LOGGER.debug(
        f"better_thermostat {self.name}: TO TRV set_local_temperature_calibration: from: {old} to: {offset}"
    )
    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": self.local_temperature_calibration_entity, "value": offset},
        blocking=True,
        limit=None,
        context=self._context,
    )
    self._last_calibration = datetime.now()


async def set_valve(self, valve):
    """Set new target valve."""
    _LOGGER.debug(f"better_thermostat {self.name}: TO TRV set_valve: {valve}")
    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": self.valve_position_entity, "value": valve},
        blocking=True,
        limit=None,
        context=self._context,
    )
