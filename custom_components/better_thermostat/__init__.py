"""The better_thermostat component."""

from asyncio import Lock
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .utils.const import (
    CONF_CALIBRATION_MODE,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_WINDOW,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    CalibrationMode,
)
from .utils.helpers import get_device_model

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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up everything this Better Thermostat instance persisted.

    Repair-registry issues are scoped by ``device_name`` or by individual
    ``entity_id`` and persist until explicitly deleted; the unified state
    store is a per-entry file that would otherwise be orphaned.
    """
    from .utils.state_manager import StateManager

    try:
        await StateManager.async_remove_store(hass, entry.entry_id)
    except Exception:
        _LOGGER.exception(
            "better_thermostat: failed to remove state store for entry %s",
            entry.entry_id,
        )

    device_name = entry.data.get(CONF_NAME, entry.title)

    for issue_id in (
        f"invalid_external_temperature_{device_name}",
        f"invalid_window_state_{device_name}",
        f"degraded_mode_{device_name}",
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    entity_ids: list[str] = []
    for trv in entry.data.get(CONF_HEATER) or []:
        trv_id = trv.get("trv")
        if trv_id:
            entity_ids.append(trv_id)
    for conf_key in (
        CONF_SENSOR,
        CONF_HUMIDITY,
        CONF_SENSOR_WINDOW,
        CONF_OUTDOOR_SENSOR,
    ):
        eid = entry.data.get(conf_key)
        if eid:
            entity_ids.append(eid)

    for eid in entity_ids:
        ir.async_delete_issue(hass, DOMAIN, f"missing_entity_{eid}")


async def async_reload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Reload the config entry."""
    await async_unload_entry(hass, config_entry)
    await async_setup_entry(hass, config_entry)


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    new = {**config_entry.data}

    if config_entry.version == 1:
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CalibrationMode.AGGRESIVE_CALIBRATION: False})

    if config_entry.version == 2:
        new[CONF_WINDOW_TIMEOUT] = 0

    if config_entry.version == 3:
        for trv in new[CONF_HEATER]:
            if (
                CalibrationMode.AGGRESIVE_CALIBRATION in trv["advanced"]
                and trv["advanced"][CalibrationMode.AGGRESIVE_CALIBRATION]
            ):
                trv["advanced"].update(
                    {CONF_CALIBRATION_MODE: CalibrationMode.AGGRESIVE_CALIBRATION}
                )
            else:
                trv["advanced"].update(
                    {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}
                )

    if config_entry.version == 4:
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CONF_NO_SYSTEM_MODE_OFF: False})

    if config_entry.version == 5:
        new[CONF_WINDOW_TIMEOUT_AFTER] = new[CONF_WINDOW_TIMEOUT]

    if config_entry.version < 18:
        # Make sure all TRVs fetch the get_device_model method to update their model info, which is used for device-specific quirks again.
        migration_context = type(
            "MigrationContext",
            (),
            {"hass": hass, "device_name": config_entry.title, "model": None},
        )()
        heaters = new.get(CONF_HEATER, [])
        for trv in heaters:
            entity_id = trv.get("entity_id")
            if entity_id:
                trv["model"] = await get_device_model(migration_context, entity_id)
                _LOGGER.debug(
                    "Migration to version 1.8: TRV %s model updated to %s",
                    entity_id,
                    trv["model"],
                )
        new[CONF_HEATER] = heaters

        _LOGGER.debug(
            "Migration to version 1.8: Updated TRV model information for all TRVs in config entry %s",
            config_entry.entry_id,
        )
        # update the new config entry with the updated TRV model information

    hass.config_entries.async_update_entry(config_entry, data=new, version=18)

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
