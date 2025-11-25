"""Device trigger helpers for Better Thermostat.

This module implements the device trigger helpers and validators used by
Home Assistant's device trigger integration for Better Thermostat climate
devices.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry

from homeassistant.components.homeassistant.triggers import (
    numeric_state as numeric_state_trigger,
    state as state_trigger,
)
from . import DOMAIN
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType
from homeassistant.components.climate.const import HVAC_MODES
from homeassistant.const import (
    CONF_ABOVE,
    CONF_BELOW,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_FOR,
    CONF_PLATFORM,
    CONF_TYPE,
    PERCENTAGE,
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List device triggers for Better Thermostat devices."""
    registry = entity_registry.async_get(hass)
    triggers = []

    # Get all the integrations entities for this device
    for entry in entity_registry.async_entries_for_device(registry, device_id):
        if entry.domain != DOMAIN:
            continue

        state = hass.states.get(entry.entity_id)
        if not state:
            continue

        base_trigger = {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_ENTITY_ID: entry.entity_id,
        }

        # Add standard climate triggers
        triggers.extend(
            [
                {
                    **base_trigger,
                    CONF_TYPE: "hvac_mode_changed",
                    "metadata": {"secondary": False},
                },
                {
                    **base_trigger,
                    CONF_TYPE: "current_temperature_changed",
                    "metadata": {"secondary": False},
                },
                {
                    **base_trigger,
                    CONF_TYPE: "current_humidity_changed",
                    "metadata": {"secondary": True},
                },
            ]
        )

    return triggers


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger."""
    if (trigger_type := config[CONF_TYPE]) == "hvac_mode_changed":
        state_config = {
            state_trigger.CONF_PLATFORM: "state",
            state_trigger.CONF_ENTITY_ID: config[CONF_ENTITY_ID],
            state_trigger.CONF_TO: config[state_trigger.CONF_TO],
            state_trigger.CONF_FROM: [
                mode for mode in HVAC_MODES if mode != config[state_trigger.CONF_TO]
            ],
        }
        if CONF_FOR in config:
            state_config[CONF_FOR] = config[CONF_FOR]
        state_config = await state_trigger.async_validate_trigger_config(
            hass, state_config
        )
        return await state_trigger.async_attach_trigger(
            hass, state_config, action, trigger_info, platform_type="device"
        )

    numeric_state_config = {
        numeric_state_trigger.CONF_PLATFORM: "numeric_state",
        numeric_state_trigger.CONF_ENTITY_ID: config[CONF_ENTITY_ID],
    }

    if trigger_type == "current_temperature_changed":
        numeric_state_config[numeric_state_trigger.CONF_VALUE_TEMPLATE] = (
            "{{ state.attributes.current_temperature }}"
        )
    else:
        numeric_state_config[numeric_state_trigger.CONF_VALUE_TEMPLATE] = (
            "{{ state.attributes.current_humidity }}"
        )

    if CONF_ABOVE in config:
        numeric_state_config[CONF_ABOVE] = config[CONF_ABOVE]
    if CONF_BELOW in config:
        numeric_state_config[CONF_BELOW] = config[CONF_BELOW]
    if CONF_FOR in config:
        numeric_state_config[CONF_FOR] = config[CONF_FOR]

    numeric_state_config = await numeric_state_trigger.async_validate_trigger_config(
        hass, numeric_state_config
    )
    return await numeric_state_trigger.async_attach_trigger(
        hass, numeric_state_config, action, trigger_info, platform_type="device"
    )


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """List trigger capabilities."""
    trigger_type = config[CONF_TYPE]

    if trigger_type == "hvac_mode_changed":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Required(state_trigger.CONF_TO): vol.In(HVAC_MODES),
                    vol.Optional(CONF_FOR): cv.positive_time_period_dict,
                }
            )
        }

    # Temperature and humidity triggers use the same schema
    if trigger_type in ["current_temperature_changed", "current_humidity_changed"]:
        unit = (
            hass.config.units.temperature_unit
            if trigger_type == "current_temperature_changed"
            else PERCENTAGE
        )
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Optional(CONF_ABOVE, description={"suffix": unit}): vol.Coerce(
                        float
                    ),
                    vol.Optional(CONF_BELOW, description={"suffix": unit}): vol.Coerce(
                        float
                    ),
                    vol.Optional(CONF_FOR): cv.positive_time_period_dict,
                }
            )
        }

    return {}
