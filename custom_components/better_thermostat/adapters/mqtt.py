import asyncio
from homeassistant.components.number.const import SERVICE_SET_VALUE
from datetime import datetime
import logging
from .generic import (
    set_temperature as generic_set_temperature,
    set_hvac_mode as generic_set_hvac_mode,
)
from ..utils.helpers import find_local_calibration_entity, find_valve_entity

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
    return await generic_set_hvac_mode(self, entity_id, hvac_mode)


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


async def set_offset(self, entity_id, offset):
    """Set new target offset."""
    max_calibration = float(
        str(
            self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).attributes.get("max", 127)
        )
    )
    min_calibration = float(
        str(
            self.hass.states.get(
                self.real_trvs[entity_id]["local_temperature_calibration_entity"]
            ).attributes.get("min", -128)
        )
    )

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
    self.real_trvs[entity_id]["last_calibration"] = datetime.now()
    await asyncio.sleep(2)
    _current_state = self.hass.states.get(entity_id).state or None
    if _current_state is not None:
        return await generic_set_hvac_mode(self, entity_id, _current_state)


async def set_valve(self, entity_id, valve):
    """Set new target valve."""
    _LOGGER.debug(f"better_thermostat {self.name}: TO TRV set_valve: {valve}")
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
