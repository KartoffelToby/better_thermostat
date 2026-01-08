"""Provide device conditions for Better Thermostat."""

from __future__ import annotations

from homeassistant.components.climate.const import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    CONF_CONDITION,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_TYPE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import condition, config_validation as cv, entity_registry
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from . import DOMAIN

CONDITION_TYPES = {"is_hvac_mode", "is_hvac_action"}

HVAC_MODE_CONDITION = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Required(CONF_TYPE): "is_hvac_mode",
        vol.Required(ATTR_HVAC_MODE): vol.In(
            [HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL]
        ),
    }
)

HVAC_ACTION_CONDITION = vol.Schema(
    {
        vol.Required(CONF_ENTITY_ID): cv.entity_id,
        vol.Required(CONF_TYPE): "is_hvac_action",
        vol.Required(ATTR_HVAC_ACTION): vol.In(
            [HVACAction.OFF, HVACAction.HEATING, HVACAction.IDLE]
        ),
    }
)

CONDITION_SCHEMA = vol.Any(HVAC_MODE_CONDITION, HVAC_ACTION_CONDITION)


async def async_get_conditions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str]]:
    """List device conditions for Better Thermostat devices."""
    registry = entity_registry.async_get(hass)
    conditions = []

    # Get all the integrations entities for this device
    for entry in entity_registry.async_entries_for_device(registry, device_id):
        if entry.domain != DOMAIN:
            continue

        base_condition = {
            CONF_CONDITION: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_ENTITY_ID: entry.entity_id,
        }

        conditions.extend(
            [{**base_condition, CONF_TYPE: cond} for cond in CONDITION_TYPES]
        )

    return conditions


@callback
def async_condition_from_config(
    hass: HomeAssistant, config: ConfigType
) -> condition.ConditionCheckerType:
    """Create a function to test a device condition."""
    if config[CONF_TYPE] == "is_hvac_mode":
        hvac_mode = config[ATTR_HVAC_MODE]

        def test_is_hvac_mode(hass: HomeAssistant, variables: dict) -> bool:
            """Test if an HVAC mode condition is met."""
            state = hass.states.get(config[CONF_ENTITY_ID])
            return (
                state is not None and state.attributes.get(ATTR_HVAC_MODE) == hvac_mode
            )

        return test_is_hvac_mode

    if config[CONF_TYPE] == "is_hvac_action":
        hvac_action = config[ATTR_HVAC_ACTION]

        def test_is_hvac_action(hass: HomeAssistant, variables: dict) -> bool:
            """Test if an HVAC action condition is met."""
            state = hass.states.get(config[CONF_ENTITY_ID])
            return (
                state is not None
                and state.attributes.get(ATTR_HVAC_ACTION) == hvac_action
            )

        return test_is_hvac_action

    return lambda *_: False


async def async_get_condition_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """List condition capabilities."""
    condition_type = config[CONF_TYPE]

    if condition_type == "is_hvac_mode":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Required(ATTR_HVAC_MODE): vol.In(
                        [HVACMode.OFF, HVACMode.HEAT, HVACMode.HEAT_COOL]
                    )
                }
            )
        }

    if condition_type == "is_hvac_action":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Required(ATTR_HVAC_ACTION): vol.In(
                        [HVACAction.OFF, HVACAction.HEATING, HVACAction.IDLE]
                    )
                }
            )
        }

    return {}
