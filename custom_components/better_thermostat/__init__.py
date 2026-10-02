"""The better_thermostat component."""

from __future__ import annotations

from asyncio import Lock
import copy
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.unit_conversion import TemperatureConverter
import voluptuous as vol

from .utils.const import (
    CONF_CALIBRATION_MODE,
    CONF_COOLER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_DOOR,
    CONF_SENSOR_WINDOW,
    CONF_WEATHER,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    DOMAIN,
    GENERIC_MODEL,
    NORMALIZED_ID_NAMES,
    CalibrationMode,
)
from .utils.helpers import get_device_model

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

RELOAD_LOCKS = f"{DOMAIN}_reload_locks"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up this integration using YAML."""
    if DOMAIN in config:
        hass.data.setdefault(DOMAIN, {})
    return True


SHARED_TRV_ISSUE_PREFIX = "shared_trv_"


def trv_entity_ids(entry: ConfigEntry) -> list[str]:
    """Return the entity ids of the thermostats ``entry`` controls."""
    heaters = entry.data.get(CONF_HEATER)
    if isinstance(heaters, str):
        return [heaters]
    return [
        trv["trv"]
        for trv in heaters or []
        if isinstance(trv, dict) and isinstance(trv.get("trv"), str) and trv["trv"]
    ]


def other_entries_controlling(
    hass: HomeAssistant, trv_entity_id: str, entry_id: str | None
) -> list[ConfigEntry]:
    """Return every entry other than ``entry_id`` that controls ``trv_entity_id``."""
    return [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry_id and trv_entity_id in trv_entity_ids(other)
    ]


def _entry_name(entry: ConfigEntry) -> str:
    return str(entry.data.get(CONF_NAME, entry.title))


def _raise_shared_trv_issue(
    hass: HomeAssistant, trv_entity_id: str, names: list[str]
) -> None:
    """Create or update the repair issue naming the entries sharing a thermostat."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{SHARED_TRV_ISSUE_PREFIX}{trv_entity_id}",
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="shared_trv",
        translation_placeholders={"trv": trv_entity_id, "entries": ", ".join(names)},
    )


def _sync_shared_trv_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Name every thermostat that more than one entry controls, and only those.

    A thermostat belongs to one entry. Entries that already share one keep
    running, so setup warns once for each thermostat this entry shares and
    keeps one repair issue per shared thermostat. An issue whose overlap is
    gone, because an entry dropped the thermostat, is deleted here as well.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : ConfigEntry
        The config entry being set up.
    """
    for trv_entity_id in trv_entity_ids(entry):
        others = other_entries_controlling(hass, trv_entity_id, entry.entry_id)
        if others:
            _LOGGER.warning(
                "better_thermostat %s: the thermostat %s is also controlled by %s; "
                "a thermostat should belong to one Better Thermostat only",
                _entry_name(entry),
                trv_entity_id,
                ", ".join(_entry_name(other) for other in others),
            )

    owners: dict[str, list[str]] = {}
    for any_entry in hass.config_entries.async_entries(DOMAIN):
        for trv_entity_id in trv_entity_ids(any_entry):
            owners.setdefault(trv_entity_id, []).append(_entry_name(any_entry))
    shared = {
        trv_entity_id: names
        for trv_entity_id, names in owners.items()
        if len(names) > 1
    }
    for trv_entity_id, names in shared.items():
        _raise_shared_trv_issue(hass, trv_entity_id, names)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(SHARED_TRV_ISSUE_PREFIX)
            and issue_id.removeprefix(SHARED_TRV_ISSUE_PREFIX) not in shared
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _warn_about_an_off_temperature_below_freezing(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Warn when a Fahrenheit entry stores an outdoor threshold below 0 °C.

    The threshold is stored in the system unit. A number meant as Celsius,
    such as 20, reads as 20 °F (-6.7 °C) on a Fahrenheit system and stops the
    heating whenever it is warmer than that outside. The stored value stays
    as it is; the warning names it so the user can change it.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : ConfigEntry
        The config entry being set up.
    """
    if hass.config.units.temperature_unit != UnitOfTemperature.FAHRENHEIT:
        return
    if not (entry.data.get(CONF_OUTDOOR_SENSOR) or entry.data.get(CONF_WEATHER)):
        return
    try:
        stored = float(entry.data[CONF_OFF_TEMPERATURE])
    except KeyError, TypeError, ValueError:
        return
    celsius = TemperatureConverter.convert(
        stored, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.CELSIUS
    )
    if celsius < 0.0:
        _LOGGER.warning(
            "better_thermostat %s: the outdoor threshold off_temperature is "
            "%s °F (%.1f °C), so heating stops whenever it is warmer than that "
            "outside; change it in the thermostat's settings if it was meant "
            "in °C",
            entry.data.get(CONF_NAME, entry.title),
            entry.data[CONF_OFF_TEMPERATURE],
            celsius,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up entry."""
    hass.data.setdefault(DOMAIN, {})
    _warn_about_an_off_temperature_below_freezing(hass, entry)
    _sync_shared_trv_issues(hass, entry)
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
            "better_thermostat: error loading platforms for entry %s", entry.entry_id
        )
        return False
    entry.async_on_unload(entry.add_update_listener(config_entry_update_listener))
    return True


def _reload_lock(hass: HomeAssistant, entry: ConfigEntry) -> Lock:
    """Return the lock one entry serializes its own reloads on.

    The lock lives on the Home Assistant instance and is keyed by entry, so
    two thermostats reload independently and a lock never outlives the
    instance it was created for. It has to survive the reload it guards,
    which is why it does not live in the per-entry data the unload clears.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : ConfigEntry
        The config entry about to reload.

    Returns
    -------
    Lock
        The lock for this entry, created on first use.
    """
    return hass.data.setdefault(RELOAD_LOCKS, {}).setdefault(entry.entry_id, Lock())


async def config_entry_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    async with _reload_lock(hass, entry):
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clean up everything this Better Thermostat instance left behind.

    Repair-registry issues are scoped by ``device_name`` or by individual
    ``entity_id`` and persist in HA's issue registry until explicitly
    deleted, so they have to be cleaned up here to avoid stale warnings
    after a config entry is gone. The reload lock and the recorded
    entity-id names outlive the entry's unload by design, so removal is
    where they are dropped.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : ConfigEntry
        The config entry being removed.
    """
    # Runtime import: events.contact imports DOMAIN from this package.
    from .events.contact import CONTACT_ROLES, contact_issue_id

    hass.data.get(RELOAD_LOCKS, {}).pop(entry.entry_id, None)
    hass.data.get(NORMALIZED_ID_NAMES, {}).pop(entry.entry_id, None)

    device_name = entry.data.get(CONF_NAME, entry.title)

    for issue_id in (
        f"invalid_external_temperature_{device_name}",
        *(contact_issue_id(role, device_name) for role in CONTACT_ROLES),
        f"degraded_mode_{device_name}",
    ):
        ir.async_delete_issue(hass, DOMAIN, issue_id)

    entity_ids: list[str] = trv_entity_ids(entry)
    for conf_key in (
        CONF_SENSOR,
        CONF_HUMIDITY,
        CONF_SENSOR_WINDOW,
        CONF_SENSOR_DOOR,
        CONF_OUTDOOR_SENSOR,
        CONF_COOLER,
    ):
        eid = entry.data.get(conf_key)
        if eid:
            entity_ids.append(eid)

    for eid in entity_ids:
        ir.async_delete_issue(hass, DOMAIN, f"missing_entity_{eid}")

    for trv_entity_id in trv_entity_ids(entry):
        remaining = other_entries_controlling(hass, trv_entity_id, entry.entry_id)
        if len(remaining) > 1:
            _raise_shared_trv_issue(
                hass, trv_entity_id, [_entry_name(other) for other in remaining]
            )
        else:
            ir.async_delete_issue(
                hass, DOMAIN, f"{SHARED_TRV_ISSUE_PREFIX}{trv_entity_id}"
            )


async def async_migrate_entry(hass, config_entry: ConfigEntry):
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if isinstance(config_entry.data.get(CONF_HEATER), str):
        _LOGGER.error(
            "better_thermostat %s: this entry was created before version "
            "1.0.0-Beta36 of the Better Thermostat integration; remove the BT "
            "devices (integration) and add it again.",
            config_entry.title,
        )
        return False

    new = copy.deepcopy(dict(config_entry.data))
    version = config_entry.version

    # Each step lifts the entry by one version, so an old entry passes every
    # step written after the one it was stored at.
    if version <= 1:
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CalibrationMode.AGGRESIVE_CALIBRATION: False})

    if version <= 2:
        new[CONF_WINDOW_TIMEOUT] = 0

    if version <= 3:
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

    if version <= 4:
        for trv in new[CONF_HEATER]:
            trv["advanced"].update({CONF_NO_SYSTEM_MODE_OFF: False})

    if version <= 5:
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
            entity_id = trv.get("trv")
            if entity_id:
                detected_model = await get_device_model(migration_context, entity_id)
                # The lookup answers GENERIC_MODEL for a device the registry
                # cannot identify, and a migration runs once, so that answer
                # must not replace a model the entry already carries. An entry
                # without one takes it: the quirks loader drives a generic
                # model with the same default module as a missing one.
                if (
                    isinstance(detected_model, str)
                    and detected_model
                    and detected_model != GENERIC_MODEL
                ) or not trv.get("model"):
                    trv["model"] = detected_model
                _LOGGER.debug(
                    "Migration to version 1.8: TRV %s carries model %s",
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
