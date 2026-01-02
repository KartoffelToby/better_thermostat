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
    CalibrationMode,
)

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]
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
    try:
        # Setup climate platform first to ensure entity is available for other platforms
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.CLIMATE])
        # Setup other platforms that depend on climate entity
        await hass.config_entries.async_forward_entry_setups(
            entry, [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]
        )
    except Exception:
        _LOGGER.exception(
            "better_thermostat: Fehler beim Laden der Plattformen für Entry %s",
            entry.entry_id,
        )
        return False
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
    """Reload the config entry."""
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)
    if config_entry.version == 1:
        new = {**config_entry.data}
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CalibrationMode.AGGRESIVE_CALIBRATION: False})
        hass.config_entries.async_update_entry(config_entry, data=new, version=2)

    if config_entry.version == 2:
        new = {**config_entry.data}
        new[CONF_WINDOW_TIMEOUT] = 0
        hass.config_entries.async_update_entry(config_entry, data=new, version=3)

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
        hass.config_entries.async_update_entry(config_entry, data=new, version=4)

    if config_entry.version == 4:
        new = {**config_entry.data}
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CONF_NO_SYSTEM_MODE_OFF: False})
        hass.config_entries.async_update_entry(config_entry, data=new, version=5)

    if config_entry.version == 5:
        new = {**config_entry.data}
        new[CONF_WINDOW_TIMEOUT_AFTER] = new[CONF_WINDOW_TIMEOUT]
        hass.config_entries.async_update_entry(config_entry, data=new, version=6)

    if config_entry.version == 6:
        new = {**config_entry.data}
        # Add ECO target temperature with default value if not present
        # ECO mode removed; preserved eco preset via PRESET_ECO - wtom: 2026-01-02
        # if "eco_temperature" not in new:
        #     new["eco_temperature"] = 18.0
        hass.config_entries.async_update_entry(config_entry, data=new, version=7)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
