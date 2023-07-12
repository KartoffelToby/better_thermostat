"""The better_thermostat component."""
import logging
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, Config
from homeassistant.config_entries import ConfigEntry
import voluptuous as vol

from .const import (
    CONF_FIX_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CONF_HEATER,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_WINDOW_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)
DOMAIN = "better_thermostat"
PLATFORMS = [Platform.CLIMATE]
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: Config):
    """Set up this integration using YAML is not supported."""
    hass.data[DOMAIN] = {}
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data[DOMAIN] = {}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(config_entry_update_listener))
    return True


async def config_entry_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
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
            trv["advanced"].update({CONF_FIX_CALIBRATION: False})
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
                CONF_FIX_CALIBRATION in trv["advanced"]
                and trv["advanced"][CONF_FIX_CALIBRATION]
            ):
                trv["advanced"].update({CONF_CALIBRATION_MODE: CONF_FIX_CALIBRATION})
            else:
                trv["advanced"].update({CONF_CALIBRATION_MODE: "default"})
        config_entry.version = 4
        hass.config_entries.async_update_entry(config_entry, data=new)

    if config_entry.version == 4:
        new = {**config_entry.data}
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CONF_NO_SYSTEM_MODE_OFF: False})
        config_entry.version = 5
        hass.config_entries.async_update_entry(config_entry, data=new)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
