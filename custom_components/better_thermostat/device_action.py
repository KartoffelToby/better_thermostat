"""Provides device actions for Better Thermostat."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_TYPE,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from . import DOMAIN
from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TEMPERATURE,
    DOMAIN as CLIMATE_DOMAIN,
    HVACMode,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
)

ACTION_TYPES = {"set_hvac_mode", "set_temperature"}

_ACTION_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(ACTION_TYPES),
        vol.Required(CONF_ENTITY_ID): cv.entity_domain(CLIMATE_DOMAIN),
    }
)


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List device actions for Better Thermostat devices."""
    registry = entity_registry.async_get(hass)
    actions = []

    # Get all the integrations entities for this device
    for entry in entity_registry.async_entries_for_device(registry, device_id):
        if entry.domain != DOMAIN:
            continue

        base_action = {
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_ENTITY_ID: entry.entity_id,
        }

        actions.extend(
            [
                {**base_action, CONF_TYPE: "set_hvac_mode"},
                {**base_action, CONF_TYPE: "set_temperature"},
            ]
        )

    return actions


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Execute a device action."""
    service_data = {ATTR_ENTITY_ID: config[CONF_ENTITY_ID]}

    if config[CONF_TYPE] == "set_hvac_mode":
        service = SERVICE_SET_HVAC_MODE
        service_data[ATTR_HVAC_MODE] = config[ATTR_HVAC_MODE]
    else:  # config[CONF_TYPE] == "set_temperature"
        service = SERVICE_SET_TEMPERATURE
        if ATTR_TARGET_TEMP_HIGH in config:
            service_data[ATTR_TARGET_TEMP_HIGH] = config[ATTR_TARGET_TEMP_HIGH]
        if ATTR_TARGET_TEMP_LOW in config:
            service_data[ATTR_TARGET_TEMP_LOW] = config[ATTR_TARGET_TEMP_LOW]
        if ATTR_TEMPERATURE in config:
            service_data[ATTR_TEMPERATURE] = config[ATTR_TEMPERATURE]

    await hass.services.async_call(
        CLIMATE_DOMAIN, service, service_data, blocking=True, context=context
    )


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """List action capabilities."""
    action_type = config[CONF_TYPE]

    if action_type == "set_hvac_mode":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Required(ATTR_HVAC_MODE): vol.In(
                        [HVACMode.HEAT, HVACMode.OFF, HVACMode.HEAT_COOL]
                    )
                }
            )
        }

    if action_type == "set_temperature":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Optional(ATTR_TEMPERATURE): vol.Coerce(float),
                    vol.Optional(ATTR_TARGET_TEMP_HIGH): vol.Coerce(float),
                    vol.Optional(ATTR_TARGET_TEMP_LOW): vol.Coerce(float),
                }
            )
        }

    return {}
