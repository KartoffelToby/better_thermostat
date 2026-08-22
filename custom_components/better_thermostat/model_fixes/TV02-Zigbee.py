"""Quirks for TV02-Zigbee thermostats."""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.model_fixes.types import ModelFixHost
from custom_components.better_thermostat.utils.helpers import (
    celsius_to_system_temperature,
)

_LOGGER = logging.getLogger(__name__)


def fix_local_calibration(self: ModelFixHost, entity_id: str, offset: float) -> float:
    """Return local calibration offset unchanged for TRV02 devices."""
    return offset


def fix_target_temperature_calibration(
    self: ModelFixHost, entity_id: str, temperature: float
) -> float:
    """Return target temperature unchanged for TRV02 devices."""
    return temperature


async def override_set_hvac_mode(
    self: ModelFixHost, entity_id: str, hvac_mode: str
) -> bool:
    """Enable device quirks while setting HVAC mode.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV.
    hvac_mode : str
        HVAC mode to be set.

    Returns
    -------
    bool
        True, always: the quirk issues the mode write itself, so the
        caller never needs the generic adapter fallback.
    """
    await self.hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": entity_id, "hvac_mode": hvac_mode},
        blocking=True,
        context=self.context,
    )
    model = self.real_trvs[entity_id].model
    if model == "TV02-Zigbee" and hvac_mode != HVACMode.OFF:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s device quirk hvac trv02-zigbee active",
            self.device_name,
            entity_id,
        )
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            context=self.context,
        )
    return True


async def override_set_temperature(
    self: ModelFixHost, entity_id: str, temperature: float
) -> bool:
    """Enable device quirks while setting temperature.

    Switches the TRV to the manual preset before the setpoint write so
    the device does not revert to its internal schedule.

    Parameters
    ----------
    self : ModelFixHost
        Better Thermostat host providing device state and HA access.
    entity_id : str
        Entity id of the TRV.
    temperature : float
        Temperature to be set, in Celsius (converted to the system unit
        before the write).

    Returns
    -------
    bool
        True, always: the quirk issues the setpoint write itself, so
        the caller never needs the generic adapter fallback.
    """
    model = self.real_trvs[entity_id].model
    if model == "TV02-Zigbee":
        _LOGGER.debug(
            "better_thermostat %s: TRV %s device quirk trv02-zigbee active",
            self.device_name,
            entity_id,
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
        {
            "entity_id": entity_id,
            "temperature": celsius_to_system_temperature(self.hass, temperature),
        },
        blocking=True,
        context=self.context,
    )
    return True
