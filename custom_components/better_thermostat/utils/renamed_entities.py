"""Carry an entry's configuration over to an entity that changed its entity id.

An entry names the entities it reads and drives by entity id. When the user
gives one of them a new id in Home Assistant, the settings, the per-thermostat
entities and the learned state still name the old one. The functions here
rewrite the first two; the learned state moves through the state manager.

The learned state of an entry has one writer at a time: its running
thermostat, which holds it in memory, or else the stored copy. A rename that
finds no running thermostat records the move here; the next one to load the
stored state under the entry's lock applies it, whether that is the rename
itself or a thermostat being set up.
"""

from __future__ import annotations

from asyncio import Lock
from collections.abc import Collection, Mapping
import copy
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_COOLER,
    CONF_DOOR_SENSORS,
    CONF_HUMIDITY_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
    CONF_WEATHER,
    CONF_WINDOW_SENSORS,
    DOMAIN,
)

if TYPE_CHECKING:
    from .state_manager import StateManager

STORED_STATE_LOCKS: Final = f"{DOMAIN}_stored_state_locks"
RECORDED_MOVES: Final = f"{DOMAIN}_recorded_thermostat_moves"

# The settings that each hold one entity id, beside the thermostat list.
ENTITY_SETTINGS: Final = (
    CONF_TEMPERATURE_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_COOLER,
    CONF_WINDOW_SENSORS,
    CONF_DOOR_SENSORS,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER,
)


def settings_with_entity_renamed(
    settings: Mapping[str, object], old_entity_id: str, new_entity_id: str
) -> dict[str, object] | None:
    """Return ``settings`` with every mention of ``old_entity_id`` replaced.

    The thermostat list and the single-entity settings are rewritten; every
    other setting, a thermostat's advanced options included, is kept as it
    is. ``settings`` itself is not changed.

    Parameters
    ----------
    settings : Mapping[str, object]
        The entry's settings as ``entry_settings`` returns them.
    old_entity_id : str
        The entity id the entity had.
    new_entity_id : str
        The entity id the entity has now.

    Returns
    -------
    dict[str, object] | None
        The rewritten settings, or ``None`` when no setting names
        ``old_entity_id``.
    """
    renamed = copy.deepcopy(dict(settings))
    found = False
    for key in ENTITY_SETTINGS:
        if renamed.get(key) == old_entity_id:
            renamed[key] = new_entity_id
            found = True
    heaters = renamed.get(CONF_THERMOSTAT)
    if isinstance(heaters, list):
        for trv_config in heaters:
            if isinstance(trv_config, dict) and trv_config.get("trv") == old_entity_id:
                trv_config["trv"] = new_entity_id
                found = True
    return renamed if found else None


def _per_trv_prefix(entry_id: str, trv_entity_id: str) -> str:
    """Return the start of the unique ids of one thermostat's own entities."""
    return f"{entry_id}_{trv_entity_id}_"


def move_trv_unique_ids(
    registry: er.EntityRegistry,
    entry_id: str,
    configured_trv_entity_ids: Collection[str],
    old_entity_id: str,
    new_entity_id: str,
) -> int:
    """Give one thermostat's own entities the unique ids of its new entity id.

    The PID numbers and switches, the child lock and the valve cap are keyed
    by the thermostat's entity id. Moving their registry rows keeps their
    entity ids, their history and the values they restore. A unique id that
    starts like the old thermostat's but belongs to another configured one,
    whose entity id merely extends the old id, stays where it is. A row
    already holding a target unique id is left over from an earlier
    configuration and is removed, since only one row can hold it.

    Parameters
    ----------
    registry : er.EntityRegistry
        The entity registry.
    entry_id : str
        The config entry, whose id starts every unique id it gives out.
    configured_trv_entity_ids : Collection[str]
        The thermostats the entry controls, before the rename.
    old_entity_id : str
        The entity id the thermostat had.
    new_entity_id : str
        The entity id the thermostat has now.

    Returns
    -------
    int
        The number of registry rows moved.
    """
    old_prefix = _per_trv_prefix(entry_id, old_entity_id)
    longer_prefixes = [
        _per_trv_prefix(entry_id, other)
        for other in configured_trv_entity_ids
        if other != old_entity_id
        and _per_trv_prefix(entry_id, other).startswith(old_prefix)
    ]
    moved = 0
    for reg_entry in list(registry.entities.get_entries_for_config_entry_id(entry_id)):
        unique_id = reg_entry.unique_id
        if (
            reg_entry.platform != DOMAIN
            or not unique_id.startswith(old_prefix)
            or any(unique_id.startswith(prefix) for prefix in longer_prefixes)
        ):
            continue
        new_unique_id = (
            _per_trv_prefix(entry_id, new_entity_id) + unique_id[len(old_prefix) :]
        )
        stale_entity_id = registry.async_get_entity_id(
            reg_entry.domain, DOMAIN, new_unique_id
        )
        if stale_entity_id is not None:
            registry.async_remove(stale_entity_id)
        registry.async_update_entity(reg_entry.entity_id, new_unique_id=new_unique_id)
        moved += 1
    return moved


def stored_state_lock(hass: HomeAssistant, entry_id: str) -> Lock:
    """Return the lock that one entry's stored state is loaded under.

    A thermostat being set up holds it from loading the stored state until
    it holds that state in memory, and a rename holds it while it moves the
    stored state. Neither reads a store the other is about to replace.
    """
    return hass.data.setdefault(STORED_STATE_LOCKS, {}).setdefault(entry_id, Lock())


def record_thermostat_move(
    hass: HomeAssistant, entry_id: str, old_entity_id: str, new_entity_id: str
) -> None:
    """Record that the stored state of ``old_entity_id`` belongs to the new id.

    Moves are applied in the order they were recorded, so two renames in a
    row end under the last id.
    """
    moves = hass.data.setdefault(RECORDED_MOVES, {}).setdefault(entry_id, [])
    moves.append((old_entity_id, new_entity_id))


def has_recorded_moves(hass: HomeAssistant, entry_id: str) -> bool:
    """Return whether moves are recorded for ``entry_id`` and not applied yet."""
    return bool(hass.data.get(RECORDED_MOVES, {}).get(entry_id))


def apply_recorded_moves(
    hass: HomeAssistant, entry_id: str, state_mgr: StateManager
) -> list[str]:
    """Apply and drop the moves recorded for ``entry_id``.

    Call with :func:`stored_state_lock` held, on a state manager that has
    just loaded the stored state.

    Returns
    -------
    list[str]
        The entity ids the state was moved to.
    """
    moves = hass.data.get(RECORDED_MOVES, {}).pop(entry_id, [])
    for old_entity_id, new_entity_id in moves:
        state_mgr.move_thermostat(old_entity_id, new_entity_id)
    return [new_entity_id for _, new_entity_id in moves]


def forget_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the lock and the recorded moves of a removed entry."""
    hass.data.get(STORED_STATE_LOCKS, {}).pop(entry_id, None)
    hass.data.get(RECORDED_MOVES, {}).pop(entry_id, None)
