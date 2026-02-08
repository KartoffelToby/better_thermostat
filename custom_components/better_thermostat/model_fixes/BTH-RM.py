"""Quirks for Bosch BTH-RM room thermostat.

Provides small fixes and device behavior adjustments required for the
Bosch BTH-RM when operated through Home Assistant integrations.
"""

import logging

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self, entity_id, offset):
    """Return a corrected local calibration offset for BTH-RM.

    The BTH-RM does not require special rounding adjustments, so this
    function is a passthrough for future extensibility.
    """
    return offset


def fix_target_temperature_calibration(self, entity_id, temperature):
    """Return a corrected target temperature calibration.

    For the BTH-RM this is currently a no-op.
    """
    return temperature


async def override_set_hvac_mode(self, entity_id, hvac_mode):
    """No special HVAC mode override for BTH-RM."""
    return False


async def override_set_temperature(self, entity_id, temperature):
    """Handle BTH-RM set_temperature quirk.

    If the device reports both 'heat' and 'cool' modes, call set_temperature
    with both `target_temp_high` and `target_temp_low` set to the same value.
    """
    model = self.real_trvs[entity_id]["model"]
    if model == "BTH-RM":
        _LOGGER.debug(
            "better_thermostat %s: TRV %s device quirk "
            "bth-rm for set_temperature active",
            self.name,
            entity_id,
        )
        entity_reg = er.async_get(self.hass)
        entry = entity_reg.async_get(entity_id)

        hvac_modes = entry.capabilities.get("hvac_modes", [])

        _LOGGER.debug(
            "better_thermostat %s: TRV %s device quirk "
            "bth-rm found hvac_modes %s",
            self.name,
            entity_id,
            hvac_modes,
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
