"""Diagnostics support for Better Thermostat."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State

from .utils.const import CONF_HEATER, CONF_SENSOR, CONF_SENSOR_WINDOW


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    trvs = {}
    for trv_config in config_entry.data[CONF_HEATER]:
        trv_state = hass.states.get(trv_config["trv"])
        if trv_state is None:
            continue
        integration = trv_config.get("integration")
        trvs[trv_config["trv"]] = {
            "name": trv_state.name,
            "state": trv_state.state,
            "attributes": trv_state.attributes,
            "bt_config": trv_config.get("advanced"),
            "bt_adapter": integration if integration is not None else "unknown",
            "bt_integration": integration,
            "model": trv_config.get("model"),
        }
    sensor_entity_id = config_entry.data.get(CONF_SENSOR)
    external_temperature = (
        hass.states.get(sensor_entity_id) if sensor_entity_id else None
    )

    window: str | State | None = "-"
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

    # Flight recorder: the last decision tuples for offline replay.
    # An entry that is not loaded has no runtime data.
    runtime_data = getattr(config_entry, "runtime_data", None)
    bt = runtime_data.climate if runtime_data is not None else None
    recorder = getattr(bt, "flight_recorder", None)
    if recorder is not None:
        diagnostics_data["flight_recorder"] = recorder.export()

    return diagnostics_data
