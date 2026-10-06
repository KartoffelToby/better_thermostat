"""Diagnostics support for Better Thermostat."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as ha_version
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .utils.const import (
    CONF_COOLER,
    CONF_DOOR_SENSORS,
    CONF_HUMIDITY_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
    CONF_WEATHER,
    CONF_WINDOW_SENSORS,
    VERSION,
)
from .utils.helpers import entry_settings

# Attributes an integration may publish on its climate or sensor entities
# that identify hardware or a place. The download is attached to public
# issues, so they are redacted wherever they appear in it.
TO_REDACT = {
    "ieee",
    "ieee_address",
    "mac",
    "mac_address",
    "ip",
    "ip_address",
    "serial",
    "serial_number",
    "latitude",
    "longitude",
}

# Configured entities besides the room sensor and the window sensor, which
# keep their own top-level keys.
_SENSOR_KEYS = (
    CONF_HUMIDITY_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_DOOR_SENSORS,
    CONF_COOLER,
    CONF_WEATHER,
)


def _state(hass: HomeAssistant, entity_id: object) -> dict[str, Any] | None:
    """Return the state of ``entity_id`` as a dict, or None without one.

    The context is left out: it names the user who triggered the change and
    links to logbook entries, and a bug report gains nothing from either.
    """
    if not isinstance(entity_id, str) or not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    facts = dict(state.as_dict())
    facts.pop("context", None)
    return facts


def _device(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """Return the integration and device-registry facts behind ``entity_id``."""
    registry_entry = er.async_get(hass).async_get(entity_id)
    if registry_entry is None:
        return None
    facts: dict[str, Any] = {"integration": registry_entry.platform}
    if registry_entry.device_id is not None:
        device = dr.async_get(hass).async_get(
            registry_entry.device_id,
            include_child_devices=False,
            include_composite_devices=False,
        )
        if device is not None:
            facts.update(
                manufacturer=device.manufacturer,
                model=device.model,
                model_id=device.model_id,
                sw_version=device.sw_version,
                hw_version=device.hw_version,
            )
    return facts


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    settings = entry_settings(config_entry)
    trvs = {}
    for trv_config in settings[CONF_THERMOSTAT]:
        trv_state = hass.states.get(trv_config["trv"])
        if trv_state is None:
            continue
        integration = trv_config.get("integration")
        trvs[trv_config["trv"]] = {
            "name": trv_state.name,
            "state": trv_state.state,
            "attributes": dict(trv_state.attributes),
            "bt_config": trv_config.get("advanced"),
            "bt_adapter": integration if integration is not None else "unknown",
            "bt_integration": integration,
            "model": trv_config.get("model"),
            "device": _device(hass, trv_config["trv"]),
        }

    _cleaned_data = dict(settings)
    del _cleaned_data[CONF_THERMOSTAT]
    diagnostics_data: dict[str, Any] = {
        "versions": {"better_thermostat": VERSION, "home_assistant": ha_version},
        "info": _cleaned_data,
        "thermostat": trvs,
        "external_temperature_sensor": _state(
            hass, settings.get(CONF_TEMPERATURE_SENSOR)
        ),
        "window_sensor": _state(hass, settings.get(CONF_WINDOW_SENSORS)),
        "sensors": {
            key: _state(hass, settings[key])
            for key in _SENSOR_KEYS
            if settings.get(key)
        },
    }

    # An entry that is not loaded has no runtime data.
    runtime_data = getattr(config_entry, "runtime_data", None)
    bt = runtime_data.climate if runtime_data is not None else None
    if bt is not None:
        # What the thermostat itself reports: mode, targets and the
        # annunciation attributes.
        diagnostics_data["climate"] = _state(hass, getattr(bt, "entity_id", None))
        # Flight recorder: the last decision tuples for offline replay.
        recorder = getattr(bt, "flight_recorder", None)
        if recorder is not None:
            diagnostics_data["flight_recorder"] = recorder.export()

    return async_redact_data(diagnostics_data, TO_REDACT)
