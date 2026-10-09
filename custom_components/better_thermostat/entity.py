"""Entity code shared by the Better Thermostat platforms."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
from typing import TYPE_CHECKING, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_registry import (
    EntityRegistry,
    async_entries_for_config_entry,
    async_get as async_get_entity_registry,
    async_get_full_entity_name,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoredExtraData, RestoreEntity

from .utils.const import DOMAIN
from .utils.watcher import is_trv_available

if TYPE_CHECKING:
    from .climate import BetterThermostat

_LOGGER = logging.getLogger(__name__)

LEARNED_STATE_SIGNAL = f"{DOMAIN}_learned_state_{{}}"

# The key the last state an entity published while available is saved under.
LAST_AVAILABLE_STATE = "last_available_state"


def last_available_state(
    saved: State | None, extra: Mapping[str, object] | None
) -> State | None:
    """Return the state a restart restores from ``saved`` and its extra data.

    A state saved as unavailable carries no attributes and stands for no
    setting; the last state published while available, saved next to it,
    does.
    """
    if saved is None or saved.state != STATE_UNAVAILABLE:
        return saved
    recorded = (extra or {}).get(LAST_AVAILABLE_STATE)
    if not isinstance(recorded, dict):
        return saved
    return State.from_dict(recorded) or saved


def announce_learned_state(hass: HomeAssistant, bt_unique_id: str) -> None:
    """Tell the thermostat's entities that its learned state may have changed.

    The PID gains and the auto-tune flag live in the state store and in no
    attribute of the thermostat, so a change to them alone changes no state
    the entities showing them could follow.
    """
    async_dispatcher_send(hass, LEARNED_STATE_SIGNAL.format(bt_unique_id))


def remove_unclaimed_registry_entries(
    registry: EntityRegistry,
    entry: ConfigEntry,
    domain: str,
    live_unique_ids: Iterable[str | None],
    bt_climate: BetterThermostat,
) -> None:
    """Remove the entry's registry entries of ``domain`` no live entity claims.

    A platform builds every entity its configuration asks for when it is set
    up. A registry entry of the same entry and domain that none of them claims
    belongs to a setting the thermostat no longer has, such as another
    calibration algorithm; left in place it shows as an unavailable entity.

    That holds only while the thermostat built every TRV it is configured
    with. The entities of a TRV it failed to build are not stale, so nothing
    is removed then.
    """
    configured = {
        trv_entity_id
        for trv_config in bt_climate.all_trvs or []
        if (trv_entity_id := trv_config.get("trv"))
    }
    missing = configured - set(bt_climate.real_trvs or {})
    if missing:
        _LOGGER.debug(
            "Better Thermostat %s: keeping the %s registry entries, TRVs %s are "
            "not set up",
            bt_climate.device_name,
            domain,
            sorted(missing),
        )
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
        if "trv_name" not in self.translation_placeholders:
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


class FollowsThermostat(Entity):
    """Entity whose value is read from the thermostat when it is published.

    Nothing is polled: the entity is published again whenever the thermostat
    publishes a new state and whenever its learned state is announced.
    """

    _attr_should_poll = False
    _bt_climate: BetterThermostat

    def _follow_thermostat(self) -> None:
        """Publish this entity again whenever the thermostat's values change."""
        if self._bt_climate.entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._bt_climate.entity_id], self._on_thermostat_state
                )
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                LEARNED_STATE_SIGNAL.format(self._bt_climate.unique_id),
                self.async_write_ha_state,
            )
        )

    @callback
    def _on_thermostat_state(self, event: Event[EventStateChangedData]) -> None:
        """Publish the values the thermostat now holds."""
        self.async_write_ha_state()


class RestoresLastAvailableState(RestoreEntity):
    """Entity that restores the settings it showed while it was available.

    An entity that is unavailable when Home Assistant stops is saved as
    ``unavailable``, without attributes, and restoring that state would lose
    every setting the entity holds. The entity saves the last state it
    published while available next to it, and a restore reads that one.
    """

    _last_available_state: State | None = None

    @override
    @callback
    def _async_write_ha_state(self) -> None:
        """Publish the state, and remember it while the entity is available."""
        super()._async_write_ha_state()
        published = self.hass.states.get(self.entity_id)
        if published is not None and published.state != STATE_UNAVAILABLE:
            self._last_available_state = published

    def _extra_restore_data(self) -> dict[str, object]:
        """Return what is saved next to the state; an entity may add to it."""
        if self._last_available_state is None:
            return {}
        return {LAST_AVAILABLE_STATE: self._last_available_state.as_dict()}

    @property
    @override
    def extra_restore_state_data(self) -> RestoredExtraData:
        """Save the last available state next to the published one."""
        return RestoredExtraData(self._extra_restore_data())

    async def async_get_last_available_state(self) -> State | None:
        """Return the state to restore: the last one published while available.

        It is also what is saved again should the entity stay unavailable
        until the next stop.
        """
        extra = await self.async_get_last_extra_data()
        restored = last_available_state(
            await self.async_get_last_state(), extra.as_dict() if extra else None
        )
        if restored is not None and restored.state != STATE_UNAVAILABLE:
            self._last_available_state = restored
        return restored


class ControlsOneTrv(Entity):
    """Entity that sets something on one TRV, usable only while the TRV is.

    It is unavailable while the TRV is, and published again whenever the
    TRV's availability changes.
    """

    _bt_climate: BetterThermostat
    _trv_entity_id: str

    @property
    @override
    def available(self) -> bool:
        """Return whether the entity and the TRV it sets are available."""
        return super().available and is_trv_available(
            self._bt_climate, self._trv_entity_id
        )

    def _follow_trv_availability(self) -> None:
        """Publish the entity again whenever the TRV comes or goes."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._trv_entity_id], self._on_trv_availability
            )
        )

    @callback
    def _on_trv_availability(self, event: Event[EventStateChangedData]) -> None:
        publish_when_availability_changed(self)


def publish_when_availability_changed(entity: Entity) -> None:
    """Publish ``entity`` when its availability differs from what it shows."""
    published = entity.hass.states.get(entity.entity_id)
    shown_available = published is not None and published.state != STATE_UNAVAILABLE
    if shown_available != entity.available:
        entity.async_write_ha_state()
