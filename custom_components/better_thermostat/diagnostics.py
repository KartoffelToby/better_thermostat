"""Diagnostics support for Better Thermostat."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State

from .utils.const import CONF_HEATER, CONF_SENSOR, CONF_SENSOR_WINDOW
from .utils.helpers import entry_settings


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    settings = entry_settings(config_entry)
    trvs = {}
    for trv_id in settings[CONF_HEATER]:
        trv = hass.states.get(trv_id["trv"])
        if trv is None:
            continue
        integration = trv_id.get("integration")
        trvs[trv_id["trv"]] = {
            "name": trv.name,
            "state": trv.state,
            "attributes": trv.attributes,
            "bt_config": trv_id.get("advanced"),
            "bt_adapter": integration if integration is not None else "unknown",
            "bt_integration": integration,
            "model": trv_id.get("model"),
        }
    sensor_entity_id = settings.get(CONF_SENSOR)
    external_temperature = (
        hass.states.get(sensor_entity_id) if sensor_entity_id else None
    )

    window: str | State | None = "-"
    window_entity_id = settings.get(CONF_SENSOR_WINDOW, False)
    if window_entity_id:
        try:
            window = hass.states.get(window_entity_id)
        except KeyError:
            pass

    _cleaned_data = dict(settings)
    del _cleaned_data[CONF_HEATER]
    diagnostics_data = {
        "info": _cleaned_data,
        "thermostat": trvs,
        "external_temperature_sensor": external_temperature,
        "window_sensor": window,
    }

    return diagnostics_data
