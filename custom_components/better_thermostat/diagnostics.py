"""Diagnostics support for Brother."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .utils.bridge import load_adapter

from .const import CONF_HEATER, CONF_SENSOR, CONF_SENSOR_WINDOW


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    trvs = {}
    for trv_id in config_entry.data[CONF_HEATER]:
        trv = hass.states.get(trv_id["trv"])
        if trv is None:
            continue
        _adapter_name = load_adapter(hass, trv_id["integration"], trv_id["trv"], True)
        trv_id["adapter"] = _adapter_name
        trvs[trv_id["trv"]] = {
            "name": trv.name,
            "state": trv.state,
            "attributes": trv.attributes,
            "bt_config": trv_id["advanced"],
            "bt_adapter": trv_id["adapter"],
            "bt_integration": trv_id["integration"],
            "model": trv_id["model"],
        }
    external_temperature = hass.states.get(config_entry.data[CONF_SENSOR])

    window = "-"
    window_entity_id = config_entry.data.get(CONF_SENSOR_WINDOW, False)
    if window_entity_id:
        try:
            window = hass.states.get(window_entity_id)
        except KeyError:
            pass

    _cleaned_data = dict(config_entry.data.copy())
    del _cleaned_data[CONF_HEATER]
    diagnostics_data = {
        "info": _cleaned_data,
        "thermostat": trvs,
        "external_temperature_sensor": external_temperature,
        "window_sensor": window,
    }

    return diagnostics_data
