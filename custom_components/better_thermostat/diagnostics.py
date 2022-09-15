"""Diagnostics support for Brother."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HEATER, CONF_SENSOR, CONF_SENSOR_WINDOW


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    trv = hass.states.get(config_entry.data[CONF_HEATER])
    external_temperature = hass.states.get(config_entry.data[CONF_SENSOR])
    if CONF_SENSOR_WINDOW in config_entry.data:
        window = hass.states.get(config_entry.data[CONF_SENSOR_WINDOW])
    else:
        window = "-"

    diagnostics_data = {
        "info": dict(config_entry.data),
        "thermostat": trv,
        "external_temperature_sensor": external_temperature,
        "window_sensor": window,
    }

    return diagnostics_data
