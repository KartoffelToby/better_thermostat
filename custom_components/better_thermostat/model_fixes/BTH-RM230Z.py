# Quirks for BTH-RM230Z
"""Model quirks for BTH-RM230Z thermostats.

Contains small device-specific fixes and overrides necessary for
compatibility with the Better Thermostat integration.
"""

import logging

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Return corrected local calibration offset for BTH-RM230Z.

    Currently a passthrough, but provided for future adjustments.
    """
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return corrected target temperature for BTH-RM230Z.

    Currently a passthrough, but provided for future adjustments.
    """
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No special HVAC mode override for BTH-RM230Z."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Handle BTH-RM230Z set_temperature quirk.

    If the device supports both 'heat' and 'cool', send target_temp_high and
    target_temp_low instead of a single temperature value.
    """
    model = self.real_trvs[entity_id]["model"]
    if model == "BTH-RM230Z":
        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} device quirk bth-rm230z for set_temperature active"
        )
        entity_reg = er.async_get(self.hass)
        entry = entity_reg.async_get(entity_id)

        hvac_modes = entry.capabilities.get("hvac_modes", [])

        _LOGGER.debug(
            f"better_thermostat {self.device_name}: TRV {entity_id} device quirk bth-rm230z found hvac_modes {hvac_modes}"
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
