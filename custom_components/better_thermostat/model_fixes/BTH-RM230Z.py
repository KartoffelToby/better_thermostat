# Quirks for BTH-RM230Z
import logging
from homeassistant.helpers import device_registry as dr, entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Bosch room thermostat BTH-RM230Z has a quirk where it needs to set both high
    and low temperature, if heat and cool modes are available in newer Z2M versions.
    """
    model = self.real_trvs[entity_id]["model"]
    if model == "BTH-RM230Z":
        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} device quirk bth-rm230z for set_temperature active"
        )
        entity_reg = er.async_get(self.hass)
        entry = entity_reg.async_get(entity_id)

        hvac_modes = entry.capabilities.get("hvac_modes", [])

        _LOGGER.debug(
            f"better_thermostat {self.name}: TRV {entity_id} device quirk bth-rm230z found hvac_modes {hvac_modes}"
        )

        if entry.platform == "mqtt" and "cool" in hvac_modes and "heat" in hvac_modes:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {
                    "entity_id": entity_id,
                    "target_temp_high": temperature,
                    "target_temp_low": temperature,
                },
                blocking=True,
                context=self.context,
            )
        else:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": entity_id, "temperature": temperature},
                blocking=True,
                context=self.context,
            )
    return True
