"""Device trigger helpers for Better Thermostat.

This module implements the device trigger helpers and validators used by
Home Assistant's device trigger integration for Better Thermostat climate
devices.

Purpose-specific triggers (HA 2025.12+):
  - heating_active          hvac_action becomes "heating"
  - heating_stopped         hvac_action leaves "heating"
  - humidity_high           humidity exceeds a configurable threshold (default 60 %)
  - window_opened           window_open attribute becomes True
  - window_closed           window_open attribute becomes False
  - battery_low             minimum TRV battery level drops below threshold (default 20 %)
  - device_error            at least one device error is present
  - target_temp_reached     current temperature reaches the target temperature

Classic triggers (kept for backwards compatibility):
  - hvac_mode_changed
  - current_temperature_changed
  - current_humidity_changed
"""

from __future__ import annotations

from homeassistant.components.climate.const import HVAC_MODES
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import (
    numeric_state as numeric_state_trigger,
    state as state_trigger,
)
from homeassistant.components.homeassistant.triggers.state import CONF_FROM, CONF_TO
from homeassistant.const import (
    CONF_ABOVE,
    CONF_ATTRIBUTE,
    CONF_BELOW,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_FOR,
    CONF_PLATFORM,
    CONF_TYPE,
    PERCENTAGE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from . import DOMAIN

# ---------------------------------------------------------------------------
# All supported trigger types
# ---------------------------------------------------------------------------

# Purpose-specific (new in HA 2025.12)
_PURPOSE_TRIGGER_TYPES = {
    "heating_active",
    "heating_stopped",
    "humidity_high",
    "window_opened",
    "window_closed",
    "battery_low",
    "device_error",
    "target_temp_reached",
}

# Legacy triggers (kept for backwards compatibility)
_CLASSIC_TRIGGER_TYPES = {
    "hvac_mode_changed",
    "current_temperature_changed",
    "current_humidity_changed",
}

TRIGGER_TYPES = _PURPOSE_TRIGGER_TYPES | _CLASSIC_TRIGGER_TYPES

# ---------------------------------------------------------------------------
# Static TRIGGER_SCHEMA required by HA 2025.12 device automation framework.
# Extra fields are validated dynamically via async_get_trigger_capabilities.
# ---------------------------------------------------------------------------
TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        # Fields used by classic triggers
        vol.Optional(CONF_TO): vol.Any(str, [str]),
        # Fields used by numeric triggers
        vol.Optional(CONF_ABOVE): vol.Coerce(float),
        vol.Optional(CONF_BELOW): vol.Coerce(float),
        # Shared optional field
        vol.Optional(CONF_FOR): cv.positive_time_period_dict,
    }
)

# ---------------------------------------------------------------------------
# Default threshold values
# ---------------------------------------------------------------------------
DEFAULT_HUMIDITY_THRESHOLD = 60.0  # %
DEFAULT_BATTERY_THRESHOLD = 20.0  # %
# Temperature delta at which "target reached" fires (current - target >= value)
TARGET_REACHED_DELTA = 0.0  # °C / °F


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, str | dict[str, bool]]]:
    """List device triggers for Better Thermostat devices."""
    registry = entity_registry.async_get(hass)
    triggers: list[dict[str, str | dict[str, bool]]] = []

    for entry in entity_registry.async_entries_for_device(registry, device_id):
        if entry.domain != DOMAIN:
            continue

        if not hass.states.get(entry.entity_id):
            continue

        base = {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_ENTITY_ID: entry.entity_id,
        }

        # ------------------------------------------------------------------
        # Purpose-specific triggers (primary – shown first in the UI)
        # ------------------------------------------------------------------
        primary_types = [
            "heating_active",
            "heating_stopped",
            "window_opened",
            "window_closed",
            "target_temp_reached",
            "device_error",
        ]
        for trigger_type in primary_types:
            triggers.append(
                {**base, CONF_TYPE: trigger_type, "metadata": {"secondary": False}}
            )

        # ------------------------------------------------------------------
        # Purpose-specific triggers (secondary – sensor / diagnostic info)
        # ------------------------------------------------------------------
        secondary_types = ["humidity_high", "battery_low"]
        for trigger_type in secondary_types:
            triggers.append(
                {**base, CONF_TYPE: trigger_type, "metadata": {"secondary": True}}
            )

        # ------------------------------------------------------------------
        # Classic / legacy triggers
        # ------------------------------------------------------------------
        triggers.extend(
            [
                {
                    **base,
                    CONF_TYPE: "hvac_mode_changed",
                    "metadata": {"secondary": True},
                },
                {
                    **base,
                    CONF_TYPE: "current_temperature_changed",
                    "metadata": {"secondary": True},
                },
                {
                    **base,
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
    """Attach a trigger and return an unsubscribe callback."""
    trigger_type: str = config[CONF_TYPE]
    entity_id: str = config[CONF_ENTITY_ID]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_state(
        attribute: str, to: str | None = None, from_: str | None = None
    ) -> dict:
        cfg: dict = {
            state_trigger.CONF_PLATFORM: "state",
            state_trigger.CONF_ENTITY_ID: entity_id,
            CONF_ATTRIBUTE: attribute,
        }
        if to is not None:
            cfg[CONF_TO] = to
        if from_ is not None:
            cfg[CONF_FROM] = from_
        if CONF_FOR in config:
            cfg[CONF_FOR] = config[CONF_FOR]
        return cfg

    def _build_numeric(template: str) -> dict:
        cfg: dict = {
            numeric_state_trigger.CONF_PLATFORM: "numeric_state",
            numeric_state_trigger.CONF_ENTITY_ID: entity_id,
            numeric_state_trigger.CONF_VALUE_TEMPLATE: template,
        }
        if CONF_ABOVE in config:
            cfg[CONF_ABOVE] = config[CONF_ABOVE]
        if CONF_BELOW in config:
            cfg[CONF_BELOW] = config[CONF_BELOW]
        if CONF_FOR in config:
            cfg[CONF_FOR] = config[CONF_FOR]
        return cfg

    # ------------------------------------------------------------------
    # Purpose-specific trigger: heating_active
    #   Fires when hvac_action changes TO "heating".
    # ------------------------------------------------------------------
    if trigger_type == "heating_active":
        state_config = _build_state("hvac_action", to="heating")
        state_config = await state_trigger.async_validate_trigger_config(
            hass, state_config
        )
        return await state_trigger.async_attach_trigger(
            hass, state_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: heating_stopped
    #   Fires when hvac_action changes FROM "heating" to anything else.
    # ------------------------------------------------------------------
    if trigger_type == "heating_stopped":
        state_config = _build_state("hvac_action", from_="heating")
        state_config = await state_trigger.async_validate_trigger_config(
            hass, state_config
        )
        return await state_trigger.async_attach_trigger(
            hass, state_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: window_opened
    #   Fires when window_open attribute becomes truthy (True).
    #   Uses a numeric template to avoid bool→string comparison issues.
    # ------------------------------------------------------------------
    if trigger_type == "window_opened":
        numeric_config = {
            numeric_state_trigger.CONF_PLATFORM: "numeric_state",
            numeric_state_trigger.CONF_ENTITY_ID: entity_id,
            numeric_state_trigger.CONF_VALUE_TEMPLATE: (
                "{{ 1 if state.attributes.get('window_open') else 0 }}"
            ),
            CONF_ABOVE: 0.5,
        }
        if CONF_FOR in config:
            numeric_config[CONF_FOR] = config[CONF_FOR]
        numeric_config = await numeric_state_trigger.async_validate_trigger_config(
            hass, numeric_config
        )
        return await numeric_state_trigger.async_attach_trigger(
            hass, numeric_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: window_closed
    #   Fires when window_open attribute becomes falsy (False / None).
    # ------------------------------------------------------------------
    if trigger_type == "window_closed":
        numeric_config = {
            numeric_state_trigger.CONF_PLATFORM: "numeric_state",
            numeric_state_trigger.CONF_ENTITY_ID: entity_id,
            numeric_state_trigger.CONF_VALUE_TEMPLATE: (
                "{{ 1 if state.attributes.get('window_open') else 0 }}"
            ),
            CONF_BELOW: 0.5,
        }
        if CONF_FOR in config:
            numeric_config[CONF_FOR] = config[CONF_FOR]
        numeric_config = await numeric_state_trigger.async_validate_trigger_config(
            hass, numeric_config
        )
        return await numeric_state_trigger.async_attach_trigger(
            hass, numeric_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: humidity_high
    #   Fires when the BT humidity attribute exceeds the threshold.
    #   Threshold is configurable (CONF_ABOVE); default is DEFAULT_HUMIDITY_THRESHOLD.
    # ------------------------------------------------------------------
    if trigger_type == "humidity_high":
        numeric_config = _build_numeric(
            "{{ state.attributes.get('humidity', 0) | float(0) }}"
        )
        if CONF_ABOVE not in numeric_config:
            numeric_config[CONF_ABOVE] = DEFAULT_HUMIDITY_THRESHOLD
        numeric_config = await numeric_state_trigger.async_validate_trigger_config(
            hass, numeric_config
        )
        return await numeric_state_trigger.async_attach_trigger(
            hass, numeric_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: battery_low
    #   Fires when the minimum TRV battery level drops below the threshold.
    #   Threshold is configurable (CONF_BELOW); default is DEFAULT_BATTERY_THRESHOLD.
    #   Template extracts the minimum 'battery' value from the batteries JSON dict.
    # ------------------------------------------------------------------
    if trigger_type == "battery_low":
        battery_template = (
            "{%- set bat = state.attributes.get('batteries', '{}') | from_json -%}"
            "{%- set levels = bat.values() | map(attribute='battery') | reject('none') | list -%}"
            "{{ (levels | min) if levels else 101 }}"
        )
        numeric_config = _build_numeric(battery_template)
        if CONF_BELOW not in numeric_config:
            numeric_config[CONF_BELOW] = DEFAULT_BATTERY_THRESHOLD
        numeric_config = await numeric_state_trigger.async_validate_trigger_config(
            hass, numeric_config
        )
        return await numeric_state_trigger.async_attach_trigger(
            hass, numeric_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: device_error
    #   Fires when the errors attribute contains at least one entry.
    # ------------------------------------------------------------------
    if trigger_type == "device_error":
        error_template = (
            "{{ (state.attributes.get('errors', '[]') | from_json | length) }}"
        )
        numeric_config = {
            numeric_state_trigger.CONF_PLATFORM: "numeric_state",
            numeric_state_trigger.CONF_ENTITY_ID: entity_id,
            numeric_state_trigger.CONF_VALUE_TEMPLATE: error_template,
            CONF_ABOVE: 0,
        }
        if CONF_FOR in config:
            numeric_config[CONF_FOR] = config[CONF_FOR]
        numeric_config = await numeric_state_trigger.async_validate_trigger_config(
            hass, numeric_config
        )
        return await numeric_state_trigger.async_attach_trigger(
            hass, numeric_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Purpose-specific trigger: target_temp_reached
    #   Fires when current_temperature >= target_temperature.
    #   The template computes (current - target); triggers when value >= TARGET_REACHED_DELTA.
    # ------------------------------------------------------------------
    if trigger_type == "target_temp_reached":
        reached_template = (
            "{{ (state.attributes.get('current_temperature', 0) | float(0))"
            " - (state.attributes.get('temperature', 0) | float(0)) }}"
        )
        numeric_config = {
            numeric_state_trigger.CONF_PLATFORM: "numeric_state",
            numeric_state_trigger.CONF_ENTITY_ID: entity_id,
            numeric_state_trigger.CONF_VALUE_TEMPLATE: reached_template,
            CONF_ABOVE: TARGET_REACHED_DELTA,
        }
        if CONF_FOR in config:
            numeric_config[CONF_FOR] = config[CONF_FOR]
        numeric_config = await numeric_state_trigger.async_validate_trigger_config(
            hass, numeric_config
        )
        return await numeric_state_trigger.async_attach_trigger(
            hass, numeric_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Classic trigger: hvac_mode_changed
    # ------------------------------------------------------------------
    if trigger_type == "hvac_mode_changed":
        state_config = {
            state_trigger.CONF_PLATFORM: "state",
            state_trigger.CONF_ENTITY_ID: entity_id,
            CONF_TO: config[CONF_TO],
            CONF_FROM: [mode for mode in HVAC_MODES if mode != config[CONF_TO]],
        }
        if CONF_FOR in config:
            state_config[CONF_FOR] = config[CONF_FOR]
        state_config = await state_trigger.async_validate_trigger_config(
            hass, state_config
        )
        return await state_trigger.async_attach_trigger(
            hass, state_config, action, trigger_info, platform_type="device"
        )

    # ------------------------------------------------------------------
    # Classic triggers: current_temperature_changed / current_humidity_changed
    # ------------------------------------------------------------------
    if trigger_type == "current_temperature_changed":
        template = "{{ state.attributes.current_temperature }}"
    else:
        template = "{{ state.attributes.current_humidity }}"

    numeric_config = _build_numeric(template)
    numeric_config = await numeric_state_trigger.async_validate_trigger_config(
        hass, numeric_config
    )
    return await numeric_state_trigger.async_attach_trigger(
        hass, numeric_config, action, trigger_info, platform_type="device"
    )


async def async_get_trigger_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """List trigger capabilities (extra fields shown in the automation editor)."""
    trigger_type = config[CONF_TYPE]

    # ------------------------------------------------------------------
    # Triggers with a "for" duration option only
    # ------------------------------------------------------------------
    if trigger_type in {
        "heating_active",
        "heating_stopped",
        "window_opened",
        "window_closed",
        "device_error",
        "target_temp_reached",
    }:
        return {
            "extra_fields": vol.Schema(
                {vol.Optional(CONF_FOR): cv.positive_time_period_dict}
            )
        }

    # ------------------------------------------------------------------
    # humidity_high: configurable threshold + duration
    # ------------------------------------------------------------------
    if trigger_type == "humidity_high":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Optional(
                        CONF_ABOVE,
                        description={"suffix": PERCENTAGE},
                        default=DEFAULT_HUMIDITY_THRESHOLD,
                    ): vol.Coerce(float),
                    vol.Optional(CONF_FOR): cv.positive_time_period_dict,
                }
            )
        }

    # ------------------------------------------------------------------
    # battery_low: configurable threshold + duration
    # ------------------------------------------------------------------
    if trigger_type == "battery_low":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Optional(
                        CONF_BELOW,
                        description={"suffix": PERCENTAGE},
                        default=DEFAULT_BATTERY_THRESHOLD,
                    ): vol.Coerce(float),
                    vol.Optional(CONF_FOR): cv.positive_time_period_dict,
                }
            )
        }

    # ------------------------------------------------------------------
    # Classic trigger: hvac_mode_changed
    # ------------------------------------------------------------------
    if trigger_type == "hvac_mode_changed":
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Required(CONF_TO): vol.In(HVAC_MODES),
                    vol.Optional(CONF_FOR): cv.positive_time_period_dict,
                }
            )
        }

    # ------------------------------------------------------------------
    # Classic triggers: temperature / humidity value thresholds
    # ------------------------------------------------------------------
    if trigger_type in {"current_temperature_changed", "current_humidity_changed"}:
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
