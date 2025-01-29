"""The better_thermostat component."""

import logging
from asyncio import Lock
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .utils.const import (
    CONF_CALIBRATION_MODE,
    CONF_HEATER,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    CONF_DOOR_TIMEOUT,  # Hinzugefügt
    CONF_DOOR_TIMEOUT_AFTER,  # Hinzugefügt
    CONF_SENSOR_DOOR,  # Hinzugefügt
    CalibrationMode,
)

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"
PLATFORMS = [Platform.CLIMATE]
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

config_entry_update_listener_lock = Lock()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up this integration using YAML."""
    if DOMAIN in config:
        hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {}
    
    # Türsensoren initialisieren
    door_sensors = entry.data.get(CONF_SENSOR_DOOR, [])
    door_timeout = entry.data.get(CONF_DOOR_TIMEOUT, 0)
    door_timeout_after = entry.data.get(CONF_DOOR_TIMEOUT_AFTER, 0)
    
    hass.data[DOMAIN][entry.entry_id][CONF_SENSOR_DOOR] = door_sensors
    hass.data[DOMAIN][entry.entry_id][CONF_DOOR_TIMEOUT] = door_timeout
    hass.data[DOMAIN][entry.entry_id][CONF_DOOR_TIMEOUT_AFTER] = door_timeout_after

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(config_entry_update_listener))
    return True


async def config_entry_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    async with config_entry_update_listener_lock:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)
    if config_entry.version == 1:
        new = {**config_entry.data}
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CalibrationMode.AGGRESIVE_CALIBRATION: False})
        config_entry.version = 2
        hass.config_entries.async_update_entry(config_entry, data=new)

    if config_entry.version == 2:
        new = {**config_entry.data}
        new[CONF_WINDOW_TIMEOUT] = 0
        config_entry.version = 3
        hass.config_entries.async_update_entry(config_entry, data=new)

    if config_entry.version == 3:
        new = {**config_entry.data}
        for trv in new[CONF_HEATER]:
            if (
                CalibrationMode.AGGRESIVE_CALIBRATION in trv["advanced"]
                and trv["advanced"][CalibrationMode.AGGRESIVE_CALIBRATION]
            ):
                trv["advanced"].update(
                    {CONF_CALIBRATION_MODE: CalibrationMode.AGGRESIVE_CALIBRATION}
                )
            else:
                trv["advanced"].update({CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT})
        config_entry.version = 4
        hass.config_entries.async_update_entry(config_entry, data=new)

    if config_entry.version == 4:
        new = {**config_entry.data}
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CONF_NO_SYSTEM_MODE_OFF: False})
        config_entry.version = 5
        hass.config_entries.async_update_entry(config_entry, data=new)

    if config_entry.version == 5:
        new = {**config_entry.data}
        new[CONF_WINDOW_TIMEOUT_AFTER] = new[CONF_WINDOW_TIMEOUT]
        config_entry.version = 6
        hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
