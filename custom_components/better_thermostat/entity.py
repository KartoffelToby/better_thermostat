"""Entity code shared by the Better Thermostat platforms."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    async_entries_for_config_entry,
    async_get as async_get_entity_registry,
    async_get_full_entity_name,
)
from homeassistant.helpers.event import async_track_state_change_event

from .utils.const import DOMAIN


def remove_unclaimed_registry_entries(
    registry: EntityRegistry,
    entry: ConfigEntry,
    domain: str,
    live_unique_ids: Iterable[str | None],
) -> None:
    """Remove the entry's registry entries of ``domain`` no live entity claims.

    A platform builds every entity its configuration asks for when it is set
    up. A registry entry of the same entry and domain that none of them claims
    belongs to a setting the thermostat no longer has, such as another
    calibration algorithm; left in place it shows as an unavailable entity.
    """
    # An unloaded registry (a mocked hass in unit tests) has no entries.
    if not hasattr(registry, "entities"):
        return
    live = set(live_unique_ids)
    for reg_entry in async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.platform == DOMAIN
            and reg_entry.domain == domain
            and reg_entry.unique_id not in live
        ):
            registry.async_remove(reg_entry.entity_id)


def current_trv_name(hass: HomeAssistant, trv_entity_id: str) -> str:
    """Return the name of a TRV as far as it is known now.

    The reported state carries the name the user sees. Before the TRV's own
    integration reports, its registry entry and device, loaded before any
    integration is set up, give the same name; a TRV without a registry
    entry is known by its entity id only. A new entity's entity_id is derived
    from its name when it is first registered, so the name matters then.
    """
    trv_state = hass.states.get(trv_entity_id)
    if trv_state is not None and trv_state.name:
        return trv_state.name
    registry = async_get_entity_registry(hass)
    reg_entry = registry.async_get(trv_entity_id)
    if reg_entry is not None:
        return async_get_full_entity_name(hass, reg_entry) or trv_entity_id
    return trv_entity_id


class TrvNamedEntity(Entity):
    """Entity of one TRV, named after it through the ``trv_name`` placeholder.

    The TRV's reported name is known once its own integration reports a
    state, which on a boot can come after this entity is built; until then
    the placeholder holds the name ``current_trv_name`` finds. The name
    follows the TRV's state from then on.
    """

    _trv_entity_id: str

    def _follow_trv_name(self) -> None:
        """Name the entity after the TRV now and whenever the TRV reports."""
        placeholders = getattr(self, "_attr_translation_placeholders", None)
        if not placeholders or "trv_name" not in placeholders:
            return
        self._adopt_trv_name(self.hass.states.get(self._trv_entity_id))
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._trv_entity_id], self._on_trv_state
            )
        )

    @callback
    def _on_trv_state(self, event: Event[EventStateChangedData]) -> None:
        """Rename the entity when the TRV reports a different name."""
        if self._adopt_trv_name(event.data["new_state"]):
            self.async_write_ha_state()

    def _adopt_trv_name(self, trv_state: State | None) -> bool:
        """Put the TRV's reported name into the placeholder.

        Returns whether the name changed. ``name`` is cached on the entity and
        not invalidated by a new placeholder, so the cache is dropped here.
        """
        if trv_state is None or not trv_state.name:
            return False
        if self._attr_translation_placeholders.get("trv_name") == trv_state.name:
            return False
        self._attr_translation_placeholders = {"trv_name": trv_state.name}
        self.__dict__.pop("name", None)
        return True
