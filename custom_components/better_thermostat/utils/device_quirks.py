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
    None
    """
    model = self.real_trvs[entity_id]["model"]
    if model == "TV02-Zigbee":
        _LOGGER.debug(f"better_thermostat {self.name}: TRV {entity_id} device quirk trv02-zigbee active")
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            limit=None,
            context=self._context,
        )

    await self.hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": entity_id, "temperature": temperature},
        blocking=True,
        limit=None,
        context=self._context,
    )


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
    None
    """
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        limit=None,
        context=self._context,
    )
    model = self.real_trvs[entity_id]["model"]
    if model == "TV02-Zigbee":
        _LOGGER.debug(f"better_thermostat {self.name}: TRV {entity_id} device quirk hvac trv02-zigbee active")
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            limit=None,
            context=self._context,
        )
