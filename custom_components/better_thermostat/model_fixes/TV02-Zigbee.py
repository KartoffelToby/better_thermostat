"""Quirks for TV02-Zigbee thermostats."""

from __future__ import annotations

import logging

from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError

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


async def _select_manual_preset(self: ModelFixHost, entity_id: str) -> None:
    """Take the TRV off its internal schedule.

    A device that refuses the preset keeps running its own schedule and
    overwrites whatever Better Thermostat sends it, so the refusal is
    reported. It does not decide the command the caller asked for: that one
    still has to reach the device, and it is written either way.
    """
    try:
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": entity_id, "preset_mode": "manual"},
            blocking=True,
            context=self.context,
        )
    except (HomeAssistantError, OSError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: TV02-Zigbee manual preset for %s failed, "
            "the device stays on its own schedule: %s",
            self.device_name,
            entity_id,
            ex,
        )


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
        True once the mode write went out, so the caller does not need the
        generic adapter fallback. False when the device refused it: the
        adapter write then carries the mode instead, with its own retry
        handling.
    """
    try:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode},
            blocking=True,
            context=self.context,
        )
    except (HomeAssistantError, OSError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: TV02-Zigbee mode write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    model = self.real_trvs[entity_id].model
    if model == "TV02-Zigbee" and hvac_mode != HVACMode.OFF:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s device quirk hvac trv02-zigbee active",
            self.device_name,
            entity_id,
        )
        await _select_manual_preset(self, entity_id)
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
        True once the setpoint write went out, so the caller does not need
        the generic adapter fallback. False when the device refused it: the
        adapter write then carries the setpoint instead, with its own step
        rounding and retry handling.
    """
    model = self.real_trvs[entity_id].model
    if model == "TV02-Zigbee":
        _LOGGER.debug(
            "better_thermostat %s: TRV %s device quirk trv02-zigbee active",
            self.device_name,
            entity_id,
        )
        await _select_manual_preset(self, entity_id)

    try:
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
    except (HomeAssistantError, OSError) as ex:
        _LOGGER.warning(
            "better_thermostat %s: TV02-Zigbee setpoint write for %s failed: %s",
            self.device_name,
            entity_id,
            ex,
        )
        return False
    return True
