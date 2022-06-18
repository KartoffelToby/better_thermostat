"""Diagnostics support for Brother."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HEATER


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    trv = hass.states.get(config_entry.data[CONF_HEATER])
    diagnostics_data = {
        "info": dict(config_entry.data),
        "thermostat": trv
    }

    return diagnostics_data
