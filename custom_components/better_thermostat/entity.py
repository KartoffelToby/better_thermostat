"""Entity code shared by the Better Thermostat platforms."""

from __future__ import annotations

from homeassistant.core import Event, EventStateChangedData, State, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event


class TrvNamedEntity(Entity):
    """Entity of one TRV, named after it through the ``trv_name`` placeholder.

    The TRV's name is known once its own integration reports a state, which on
    a boot can come after this entity is built; until then the placeholder
    holds the TRV's entity id. The name follows the TRV's state from then on.
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
