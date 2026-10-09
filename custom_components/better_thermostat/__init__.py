"""The better_thermostat component."""

from __future__ import annotations

from asyncio import Lock
import copy
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform, UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er, issue_registry as ir, service
from homeassistant.helpers.typing import ConfigType, VolDictType, VolSchemaType
from homeassistant.util.unit_conversion import TemperatureConverter
import voluptuous as vol

from .utils.const import (
    BETTERTHERMOSTAT_RESET_PID_SCHEMA,
    CONF_CALIBRATION_MODE,
    CONF_NO_OFF_SYSTEM_MODE,
    CONF_OFF_TEMPERATURE,
    CONF_OUTDOOR_SENSOR,
    CONF_THERMOSTAT,
    CONF_WEATHER,
    CONF_WINDOW_OFF_DELAY,
    CONF_WINDOW_OFF_DELAY_AFTER,
    DOMAIN,
    GENERIC_MODEL,
    NORMALIZED_ID_NAMES,
    SERVICE_RESET_HEATING_POWER,
    SERVICE_RESET_PID_LEARNINGS,
    SERVICE_RUN_VALVE_MAINTENANCE,
    CalibrationMode,
)
from .utils.entry_schema import BtSettings, InvalidSettingsError, parse_settings
from .utils.helpers import (
    async_delete_entry_issues,
    entry_name,
    entry_settings,
    get_device_model,
    setting_str,
    stored_trv_configs,
)
from .utils.renamed_entities import move_trv_unique_ids, settings_with_entity_renamed
from .utils.stored_values import stored_float

if TYPE_CHECKING:
    from .climate import BetterThermostat
    from .utils.state_manager import StateManager

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]
CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

RELOAD_LOCKS = f"{DOMAIN}_reload_locks"
STORED_STATE_LOCK = f"{DOMAIN}_stored_state_lock"


@dataclass
class BetterThermostatData:
    """What a loaded entry holds at runtime.

    The climate entity is set by the climate platform; the platforms set up
    after it find it here. ``settings`` holds the entry's settings as setup
    parsed them; it is ``None`` only for a container built without them.
    """

    climate: BetterThermostat | None = None
    settings: BtSettings | None = None


type BetterThermostatConfigEntry = ConfigEntry[BetterThermostatData]


# Service name to the climate entity method it runs, with the schema of the
# fields it takes.
_ENTITY_SERVICES: tuple[tuple[str, str, VolDictType | VolSchemaType], ...] = (
    (SERVICE_RESET_HEATING_POWER, "reset_heating_power", {}),
    (SERVICE_RUN_VALVE_MAINTENANCE, "run_valve_maintenance_service", {}),
    (
        SERVICE_RESET_PID_LEARNINGS,
        "reset_pid_learnings_service",
        BETTERTHERMOSTAT_RESET_PID_SCHEMA,
    ),
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up this integration using YAML and register its services.

    The services are registered here rather than with each entry, so they
    exist, and validate, while no thermostat is set up.
    """
    for name, method, schema in _ENTITY_SERVICES:
        service.async_register_platform_entity_service(
            hass, DOMAIN, name, entity_domain=CLIMATE_DOMAIN, func=method, schema=schema
        )
    _follow_entity_renames(hass)
    return True


SHARED_TRV_ISSUE_PREFIX = "shared_trv_"


def trv_entity_ids(entry: BetterThermostatConfigEntry) -> list[str]:
    """Return the entity ids of the thermostats ``entry`` controls.

    Every entry is read this way, loaded or not, so the settings are read
    without parsing them: a bare entity id counts as one thermostat, and a
    value or element without a non-empty entity id counts as none.
    """
    settings = entry_settings(entry)
    heaters = settings.get(CONF_THERMOSTAT)
    if isinstance(heaters, str):
        return [heaters]
    return [
        trv_entity_id
        for trv in stored_trv_configs(settings)
        if (trv_entity_id := setting_str(trv, "trv"))
    ]


def other_entries_controlling(
    hass: HomeAssistant, trv_entity_id: str, entry_id: str | None
) -> list[BetterThermostatConfigEntry]:
    """Return every entry other than ``entry_id`` that controls ``trv_entity_id``."""
    return [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry_id and trv_entity_id in trv_entity_ids(other)
    ]


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
        learn_more_url="https://better-thermostat.org/faq/shared-trv",
        translation_placeholders={"trv": trv_entity_id, "entries": ", ".join(names)},
    )


def _sync_shared_trv_issues(
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
) -> None:
    """Name every thermostat that more than one entry controls, and only those.

    A thermostat belongs to one entry. Entries that already share one keep
    running, so setup warns once for each thermostat this entry shares and
    keeps one repair issue per shared thermostat. An issue whose overlap is
    gone, because an entry dropped the thermostat, is deleted here as well.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : BetterThermostatConfigEntry
        The config entry being set up.
    """
    for trv_entity_id in trv_entity_ids(entry):
        others = other_entries_controlling(hass, trv_entity_id, entry.entry_id)
        if others:
            _LOGGER.warning(
                "better_thermostat %s: the thermostat %s is also controlled by %s; "
                "a thermostat should belong to one Better Thermostat only",
                entry_name(entry),
                trv_entity_id,
                ", ".join(entry_name(other) for other in others),
            )

    owners: dict[str, list[str]] = {}
    for any_entry in hass.config_entries.async_entries(DOMAIN):
        for trv_entity_id in trv_entity_ids(any_entry):
            owners.setdefault(trv_entity_id, []).append(entry_name(any_entry))
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
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
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
    entry : BetterThermostatConfigEntry
        The config entry being set up.
    """
    if hass.config.units.temperature_unit != UnitOfTemperature.FAHRENHEIT:
        return
    settings = entry_settings(entry)
    if not (settings.get(CONF_OUTDOOR_SENSOR) or settings.get(CONF_WEATHER)):
        return
    try:
        stored = stored_float(settings[CONF_OFF_TEMPERATURE])
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
            entry_name(entry),
            settings[CONF_OFF_TEMPERATURE],
            celsius,
        )


def _keep_settings_in_options(
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
) -> None:
    """Move settings found in the entry's data into its options.

    The migration to minor version 2 moves them once. 1.9.3 loads an entry of
    that version too and, when its settings are saved there, writes them to
    the data and empties the options without changing the version, so the
    move is repeated whenever the data holds anything.
    """
    if entry.data:
        hass.config_entries.async_update_entry(
            entry, data={}, options=entry_settings(entry)
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
) -> bool:
    """Set up entry.

    Raises
    ------
    ConfigEntryError
        When the stored settings do not have the shape the integration reads.
        The entry stays as stored, and the options flow, which reads the
        stored settings, still opens for it.
    """
    _keep_settings_in_options(hass, entry)
    try:
        settings = parse_settings(entry_settings(entry))
    except InvalidSettingsError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="invalid_settings",
            translation_placeholders={"reason": str(err)},
        ) from err
    _warn_about_an_off_temperature_below_freezing(hass, entry)
    _sync_shared_trv_issues(hass, entry)
    entry.runtime_data = BetterThermostatData(settings=settings)
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


def _reload_lock(hass: HomeAssistant, entry: BetterThermostatConfigEntry) -> Lock:
    """Return the lock one entry serializes its own reloads on.

    The lock lives on the Home Assistant instance and is keyed by entry, so
    two thermostats reload independently and a lock never outlives the
    instance it was created for. It has to survive the reload it guards,
    which is why it does not live in the entry's runtime data, which Home
    Assistant drops on unload.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : BetterThermostatConfigEntry
        The config entry about to reload.

    Returns
    -------
    Lock
        The lock for this entry, created on first use.
    """
    return hass.data.setdefault(RELOAD_LOCKS, {}).setdefault(entry.entry_id, Lock())


async def config_entry_update_listener(
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
) -> None:
    """Handle options update."""
    async with _reload_lock(hass, entry):
        await hass.config_entries.async_reload(entry.entry_id)


@callback
def _follow_entity_renames(hass: HomeAssistant) -> None:
    """Carry every entry over to an entity that is given a new entity id.

    The listener lives as long as the integration, not as long as an entry,
    so an entry that is not loaded when its entity is renamed follows the
    rename too.
    """

    async def _follow(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        for entry in hass.config_entries.async_entries(DOMAIN):
            await _follow_entity_rename(
                hass, entry, data["old_entity_id"], data["entity_id"]
            )

    hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _follow)


def _running_state_manager(entry: BetterThermostatConfigEntry) -> StateManager | None:
    """Return the state manager of ``entry``'s running thermostat, if any."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    climate = entry.runtime_data.climate
    return None if climate is None else climate.state_mgr


async def _move_stored_state(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    old_entity_id: str,
    new_entity_id: str,
) -> None:
    """Move the stored state of an entry that is not running to the new id.

    The moves of all entries share one lock, so a second rename that
    follows the first quickly loads the store the first one saved.
    """
    # Runtime import, for the reason async_remove_entry gives.
    from .utils.state_manager import StateManager  # noqa: PLC0415

    async with hass.data.setdefault(STORED_STATE_LOCK, Lock()):
        state_mgr = StateManager(hass, entry.entry_id)
        await state_mgr.load()
        state_mgr.move_thermostat(old_entity_id, new_entity_id)
        await state_mgr.save_if_dirty()
        state_mgr.close()


async def _follow_entity_rename(
    hass: HomeAssistant,
    entry: BetterThermostatConfigEntry,
    old_entity_id: str,
    new_entity_id: str,
) -> None:
    """Rewrite ``entry`` to name ``new_entity_id`` wherever it named the old id.

    A renamed thermostat keeps its advanced options, its own entities with
    their values and history, and what was learned for it. A running
    thermostat holds that state in memory and saves it when it is unloaded,
    so the state moves there, before the rewritten settings are saved and
    reload the entry under the new id. Up to that save nothing yields to the
    event loop, so renames that follow each other quickly are applied in
    order. An entry that is not running has its stored state moved instead.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : BetterThermostatConfigEntry
        A Better Thermostat config entry, which may or may not name the entity.
    old_entity_id : str
        The entity id the entity had.
    new_entity_id : str
        The entity id the entity has now.
    """
    renamed = settings_with_entity_renamed(
        entry_settings(entry), old_entity_id, new_entity_id
    )
    if renamed is None:
        return
    trvs = trv_entity_ids(entry)
    is_trv = old_entity_id in trvs
    state_mgr = _running_state_manager(entry)
    if is_trv:
        move_trv_unique_ids(
            er.async_get(hass), entry.entry_id, trvs, old_entity_id, new_entity_id
        )
        if state_mgr is not None:
            state_mgr.move_thermostat(old_entity_id, new_entity_id)
    ir.async_delete_issue(hass, DOMAIN, f"missing_entity_{old_entity_id}")
    _LOGGER.info(
        "better_thermostat %s: %s is now %s; the settings follow the new entity id",
        entry_name(entry),
        old_entity_id,
        new_entity_id,
    )
    hass.config_entries.async_update_entry(entry, data={}, options=renamed)
    if is_trv and state_mgr is None:
        await _move_stored_state(hass, entry, old_entity_id, new_entity_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
) -> bool:
    """Unload a config entry and delete the repair issues it raised.

    A reload after an options change unloads the entry too. An issue whose
    entity was taken out of the entry goes with the unload, and one that
    still applies is raised again by the reloaded thermostat.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        async_delete_entry_issues(hass, entry.entry_id)
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: BetterThermostatConfigEntry
) -> None:
    """Clean up everything this Better Thermostat instance left behind.

    The repair issues of the entry go with it, also when the entry is
    removed without having been loaded; the unified state store is a
    per-entry file that would otherwise be orphaned. The reload lock and the
    recorded entity-id names outlive the entry's unload by design, so
    removal is where they are dropped.

    Parameters
    ----------
    hass : HomeAssistant
        The running Home Assistant instance.
    entry : BetterThermostatConfigEntry
        The config entry being removed.
    """
    # Runtime import: config_flow and the three device-automation modules
    # execute this package for DOMAIN alone, on installs that may have no
    # entry set up. A module-level import would put the state store, and
    # the control kernel, calibration models and numpy behind it, on those
    # paths.
    from .utils.state_manager import StateManager  # noqa: PLC0415

    hass.data.get(RELOAD_LOCKS, {}).pop(entry.entry_id, None)
    hass.data.get(NORMALIZED_ID_NAMES, {}).pop(entry.entry_id, None)

    try:
        await StateManager.async_remove_store(hass, entry.entry_id)
    except Exception:
        _LOGGER.exception(
            "better_thermostat: failed to remove state store for entry %s",
            entry.entry_id,
        )

    async_delete_entry_issues(hass, entry.entry_id)

    for trv_entity_id in trv_entity_ids(entry):
        remaining = other_entries_controlling(hass, trv_entity_id, entry.entry_id)
        if len(remaining) > 1:
            _raise_shared_trv_issue(
                hass, trv_entity_id, [entry_name(other) for other in remaining]
            )
        else:
            ir.async_delete_issue(
                hass, DOMAIN, f"{SHARED_TRV_ISSUE_PREFIX}{trv_entity_id}"
            )


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: BetterThermostatConfigEntry
) -> bool:
    """Migrate old entry."""
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if isinstance(config_entry.data.get(CONF_THERMOSTAT), str):
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
        for trv in new[CONF_THERMOSTAT]:
            trv["advanced"].update({CalibrationMode.AGGRESSIVE_CALIBRATION: False})

    if version <= 2:
        new[CONF_WINDOW_OFF_DELAY] = 0

    if version <= 3:
        for trv in new[CONF_THERMOSTAT]:
            if (
                CalibrationMode.AGGRESSIVE_CALIBRATION in trv["advanced"]
                and trv["advanced"][CalibrationMode.AGGRESSIVE_CALIBRATION]
            ):
                trv["advanced"].update(
                    {CONF_CALIBRATION_MODE: CalibrationMode.AGGRESSIVE_CALIBRATION}
                )
            else:
                trv["advanced"].update(
                    {CONF_CALIBRATION_MODE: CalibrationMode.MPC_CALIBRATION}
                )

    if version <= 4:
        for trv in new[CONF_THERMOSTAT]:
            trv["advanced"].update({CONF_NO_OFF_SYSTEM_MODE: False})

    if version <= 5:
        new[CONF_WINDOW_OFF_DELAY_AFTER] = new[CONF_WINDOW_OFF_DELAY]

    if config_entry.version < 18:
        # Make sure all TRVs fetch the get_device_model method to update their model info, which is used for device-specific quirks again.
        migration_context = type(
            "MigrationContext", (), {"hass": hass, "device_name": config_entry.title}
        )()
        heaters = new.get(CONF_THERMOSTAT, [])
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
        new[CONF_THERMOSTAT] = heaters

        _LOGGER.debug(
            "Migration to version 1.8: Updated TRV model information for all TRVs in config entry %s",
            config_entry.entry_id,
        )
        # update the new config entry with the updated TRV model information

    # Minor version 2 keeps the settings in the options and the data empty.
    hass.config_entries.async_update_entry(
        config_entry,
        data={},
        options={**new, **config_entry.options},
        version=18,
        minor_version=2,
    )

    _LOGGER.info("Migration to version %s successful", config_entry.version)

    return True
