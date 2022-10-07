from homeassistant.components.number.const import SERVICE_SET_VALUE
from datetime import datetime, timedelta
import logging
from .generic import (
    set_temperature as generic_set_temperature,
    set_hvac_mode as generic_set_hvac_mode,
)
from ..utils.helpers import find_local_calibration_entity

_LOGGER = logging.getLogger(__name__)


def get_info():
    """Get info from TRV."""
    return {"support_offset": True, "support_valve": True}


async def init(self):
    if self.local_temperature_calibration_entity is None and self.calibration_type == 0:
        self.local_temperature_calibration_entity = await find_local_calibration_entity(
            self
        )
        _LOGGER.info(
            "better_thermostat %s: uses local calibration entity %s",
            self.name,
            self.local_temperature_calibration_entity,
        )


async def set_temperature(self, temperature):
    """Set new target temperature."""
    return await generic_set_temperature(self, temperature)


async def set_hvac_mode(self, hvac_mode):
    """Set new target hvac mode."""
    return await generic_set_hvac_mode(self, hvac_mode)


async def set_offset(self, offset):
    """Set new target offset."""
    current_calibration = self.hass.states.get(
        self.local_temperature_calibration_entity
    ).state
    if current_calibration != offset and (
        (self._last_calibration + timedelta(minutes=5)).timestamp()
        < datetime.now().timestamp()
    ):
        _LOGGER.info(f"better_thermostat {self.name}: TO TRV set_offset: {offset}")
        max_calibration = self.hass.states.get(
            self.local_temperature_calibration_entity
        ).attributes.get("max", 127)
        min_calibration = self.hass.states.get(
            self.local_temperature_calibration_entity
        ).attributes.get("min", -128)
        if offset > max_calibration:
            offset = max_calibration
        if offset < min_calibration:
            offset = min_calibration
        await self.hass.services.async_call(
            "number",
            SERVICE_SET_VALUE,
            {"entity_id": self.local_temperature_calibration_entity, "value": offset},
            blocking=True,
            limit=None,
            context=self._context,
        )
        self._last_calibration = datetime.now()
    else:
        _LOGGER.debug(
            f"better_thermostat {self.name}: set_trv_values: skipping local calibration because of throttling"
        )


async def set_valve(self, valve):
    """Set new target valve."""
    _LOGGER.info(f"better_thermostat {self.name}: TO TRV set_valve: {valve}")
    await self.hass.services.async_call(
        "number",
        SERVICE_SET_VALUE,
        {"entity_id": self.valve_position_entity, "value": valve},
        blocking=True,
        limit=None,
        context=self._context,
    )
