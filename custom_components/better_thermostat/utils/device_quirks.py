import asyncio
import logging


_LOGGER = logging.getLogger(__name__)


async def set_temperature_quirk(self, entity_id, temperature):
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
    True: Quirk was done
    False: No Quirk found
    """
    if self.real_trvs[entity_id].get("model") == "TV02-Zigbee":
        trv02_zigbee_set_temperature(self, entity_id, temperature)
        return True

    return False

async def set_hvac_mode_quirk(self, entity_id, hvac_mode):
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
    True: Quirk was done
    False: No Quirk found
    """

    return False


async def trv02_zigbee_set_temperature(self, entity_id, temperature):
    """TRV02-Zigbee needs to be set to manual, to hold the defined temperature without overriding it with the auto-schedule."""
    await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            limit=None,
            context=self._context,
        )
    await asyncio.sleep(1)
    await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": temperature},
            blocking=True,
            limit=None,
            context=self._context,
        )
