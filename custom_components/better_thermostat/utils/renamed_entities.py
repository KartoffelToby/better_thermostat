"""Carry an entry's configuration over to an entity that changed its entity id.

An entry names the entities it reads and drives by entity id. When the user
gives one of them a new id in Home Assistant, the settings, the per-thermostat
entities and the learned state still name the old one. The functions here
rewrite the first two; the learned state moves through the state manager.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
import copy
from typing import Final

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
