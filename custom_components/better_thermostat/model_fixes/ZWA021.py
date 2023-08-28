import logging
from homeassistant.components.climate.const import HVACMode
from homeassistant.components.zwave_js.const import SERVICE_SET_VALUE, DOMAIN

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    model = self.real_trvs[entity_id]["model"]
    _LOGGER.debug(f"Setting {entity_id} model {model} hvac mode to {hvac_mode}")
    if model == "ZWA021" and hvac_mode != HVACMode.OFF:
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} device quirk hvac ZWA021 active"
        )
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SET_VALUE,
            {
                "entity_id": entity_id,
                "command_class": "64",
                "property": "mode",
                "value": "31",
            },
            blocking=True,
            context=self.context,
        )
    else:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode},
            blocking=True,
            context=self.context,
        )
    return True


async def override_set_temperature(self, entity_id, temperature):
    return False
