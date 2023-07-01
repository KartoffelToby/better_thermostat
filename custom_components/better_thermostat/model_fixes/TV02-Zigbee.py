# Quirks for TV02-Zigbee
import logging
from homeassistant.components.climate.const import HVACMode

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """Enable specific device quirks while setting hvac mode
    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            Entity id of the TRV.
    hvac_mode:
            HVAC mode to be set.
    Returns
    -------
    None
    """
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        context=self.context,
    )
    model = self.real_trvs[entity_id]["model"]
    if model == "TV02-Zigbee" and hvac_mode != HVACMode.OFF:
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} device quirk hvac trv02-zigbee active"
        )
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            context=self.context,
        )
    return True


async def override_set_temperature(self, entity_id, temperature):
    """Enable specific device quirks while setting temperature
    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id :
            Entity id of the TRV.
    temperature:
            Temperature to be set.
    Returns
    -------
    None
    """
    model = self.real_trvs[entity_id]["model"]
    if model == "TV02-Zigbee":
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} device quirk trv02-zigbee active"
        )
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            context=self.context,
        )

    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": temperature},
        blocking=True,
        context=self.context,
    )
    return True
