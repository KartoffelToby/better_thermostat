"""Tests for the entity-id rename that follows a thermostat rename.

``async_normalize_bt_entity_ids`` decides, per config entry and platform,
whether the registry ids have to be rebuilt. The decision rests on a name
recorded in ``hass.data``: absent means this process has not set the
platform up before, which is a restart and leaves the ids alone. These
tests drive that decision directly and cover what the registry does to a
rename once the decision has been made in its favour.
"""

from unittest.mock import MagicMock, patch

from homeassistant.const import Platform

from custom_components.better_thermostat.utils.const import DOMAIN
from custom_components.better_thermostat.utils.helpers import (
    async_normalize_bt_entity_ids,
)


def _entry(name):
    """Build a config entry stub carrying ``name``."""
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.data = {"name": name}
    return entry


def _registry_entry(entity_id, domain):
    """Build a registry entry stub this integration owns."""
    reg_entry = MagicMock()
    reg_entry.entity_id = entity_id
    reg_entry.platform = DOMAIN
    reg_entry.domain = domain
    return reg_entry


def _registry(entries):
    """Build an entity registry stub holding ``entries``."""
    registry = MagicMock()
    registry.entities.get_entries_for_config_entry_id.return_value = entries
    return registry


def _rename(hass, entry, registry, new_name, domain=Platform.SENSOR):
    """Rename ``entry`` to ``new_name`` against ``registry``; return the calls.

    The first call records the name the entry arrives with, so it is the
    second one -- the entry now carrying ``new_name`` -- that is the rename
    under test.
    """
    with patch(
        "custom_components.better_thermostat.utils.helpers.er.async_get",
        return_value=registry,
    ):
        async_normalize_bt_entity_ids(hass, entry, domain)
        entry.data = {**entry.data, "name": new_name}
        async_normalize_bt_entity_ids(hass, entry, domain)
    return registry.async_update_entity.call_args_list


def test_a_registry_without_entities_is_left_alone():
    """An unloaded registry shell is not walked.

    The registry is populated lazily, so before the first load it carries
    no ``entities`` at all. Reaching for them would raise, and there is
    nothing to rename in an empty registry either way.
    """
    hass = MagicMock()
    hass.data = {}
    # A shell carrying everything but the ``entities`` it has not loaded yet.
    registry = MagicMock(spec=["async_update_entity", "async_regenerate_entity_id"])

    calls = _rename(hass, _entry("Livingroom"), registry, "Bedroom")

    assert calls == []


def test_an_id_that_already_matches_is_not_rewritten():
    """A rename that leaves an id unchanged writes nothing.

    Renaming "Livingroom" to "livingroom" changes the name the entry
    carries but not the slug the ids are built from, and rewriting an
    entity_id to itself is a registry error rather than a no-op.
    """
    hass = MagicMock()
    hass.data = {}
    reg_entry = _registry_entry("sensor.livingroom_temperature_ema", Platform.SENSOR)
    registry = _registry([reg_entry])
    registry.async_regenerate_entity_id.return_value = reg_entry.entity_id

    calls = _rename(hass, _entry("Livingroom"), registry, "livingroom")

    assert calls == []


def test_a_rejected_rename_is_reported_and_the_others_still_run(caplog):
    """One id the registry refuses does not cost the remaining ones.

    ``async_update_entity`` raises when the target id is taken. The loop
    walks every entity of the platform, so swallowing the rejection is
    what keeps a single collision from stopping the rename half-done.
    """
    hass = MagicMock()
    hass.data = {}
    blocked = _registry_entry("sensor.livingroom_temperature_ema", Platform.SENSOR)
    following = _registry_entry("sensor.livingroom_valve", Platform.SENSOR)
    registry = _registry([blocked, following])
    registry.async_regenerate_entity_id.side_effect = [
        "sensor.bedroom_temperature_ema",
        "sensor.bedroom_valve",
    ]
    registry.async_update_entity.side_effect = [
        ValueError("Entity id already exists"),
        None,
    ]

    calls = _rename(hass, _entry("Livingroom"), registry, "Bedroom")

    assert [call.args[0] for call in calls] == [blocked.entity_id, following.entity_id]
    assert "could not rename sensor.livingroom_temperature_ema" in caplog.text


def test_an_entity_of_another_platform_is_skipped():
    """Only the platform being set up is renamed.

    Each platform records its own name and is walked on its own pass, so
    a pass must leave the entries of the other three alone -- they are in
    the registry under the same config entry.
    """
    hass = MagicMock()
    hass.data = {}
    other = _registry_entry("switch.livingroom_child_lock", Platform.SWITCH)
    registry = _registry([other])
    registry.async_regenerate_entity_id.return_value = "switch.bedroom_child_lock"

    calls = _rename(
        hass, _entry("Livingroom"), registry, "Bedroom", domain=Platform.SENSOR
    )

    assert calls == []
