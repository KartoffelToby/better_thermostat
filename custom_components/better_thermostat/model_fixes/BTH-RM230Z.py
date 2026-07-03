"""Model quirks for BTH-RM230Z thermostats.

Contains small device-specific fixes and overrides necessary for
compatibility with the Better Thermostat integration.
"""

from __future__ import annotations

import logging

from custom_components.better_thermostat.utils.helpers import (
    trv_supports_temperature_range,
)

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

    This device exposes both a single 'temperature' setpoint and a
    'target_temp_high'/'target_temp_low' range, but its actual heating
    logic is driven by target_temp_low (the single 'temperature' field
    is effectively cosmetic when the range feature is active).

    We can't detect this via hvac_modes -- this device never lists
    'cool' even when the range feature is active -- so we check the
    live supported_features bitmask for TARGET_TEMPERATURE_RANGE
    instead, and if present, write both target_temp_high and
    target_temp_low so the device actually reacts.

    Parameters
    ----------
    self :
            self instance of better_thermostat
    entity_id : str
            entity_id of the TRV
    temperature : float
            the target temperature to set

    Returns
    -------
    bool
            True, always: the quirk issues a service call for every
            input (a plain temperature write when the entity has no
            current state or no range support, a range write
            otherwise), so the caller never needs the generic
            adapter fallback.
    """
    state = self.hass.states.get(entity_id)
    if state is None:
        _LOGGER.debug(
            "better_thermostat %s: TRV %s has no current state, "
            "falling back to simple set_temperature",
            self.device_name,
            entity_id,
        )
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": temperature},
            blocking=True,
            context=self.context,
        )
        return True

    _supports_range = trv_supports_temperature_range(state)

    _LOGGER.debug(
        f"better_thermostat {self.device_name}: TRV {entity_id} device quirk bth-rm230z "
        f"found supported_features {state.attributes.get('supported_features', 0)} (range={_supports_range})"
    )

    if _supports_range:
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
