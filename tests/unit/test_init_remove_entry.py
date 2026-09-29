"""Tests for repair-issue cleanup on config-entry removal.

Better Thermostat creates persistent ``issue_registry`` entries for various
runtime conditions (invalid sensor reading, missing entity, degraded mode).
Deleting the config entry must remove the associated repair issues so that
they do not linger after the BT instance is gone.
"""

from asyncio import Lock
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat import DOMAIN, RELOAD_LOCKS, async_remove_entry
from custom_components.better_thermostat.events.contact import DOOR, WINDOW
from custom_components.better_thermostat.utils.const import (
    CONF_COOLER,
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
    CONF_SENSOR_DOOR,
    CONF_SENSOR_WINDOW,
)


def _make_entry(**overrides):
    entry = MagicMock()
    entry.entry_id = "abcd1234"
    entry.title = "Kinderzimmer"
    data = {
        CONF_NAME: "Kinderzimmer",
        CONF_HEATER: [{"trv": "climate.fritz_kinderzimmer", "advanced": {}}],
        CONF_SENSOR: "sensor.kinderzimmer_temperature",
    }
    data.update(overrides)
    entry.data = data
    return entry


def _make_hass():
    """Return a Home Assistant double whose ``data`` is a real mapping.

    ``async_remove_entry`` reads ``hass.data`` and the config entries
    synchronously. An AsyncMock answers every attribute with a coroutine, so
    both have to be real for the removal to reach what is in them.
    """
    hass = AsyncMock()
    hass.data = {}
    # The removal asks which other entries control the same thermostats.
    hass.config_entries = MagicMock()
    hass.config_entries.async_entries.return_value = []
    return hass


@pytest.fixture
def patched_delete_issue():
    """Patch ``ir.async_delete_issue`` and return the mock for assertions."""
    with patch(
        "custom_components.better_thermostat.ir.async_delete_issue"
    ) as mock_delete:
        yield mock_delete


class TestAsyncRemoveEntryCleansRepairIssues:
    """``async_remove_entry`` must clean up every repair-issue pattern BT creates."""

    @pytest.mark.asyncio
    async def test_deletes_device_name_keyed_issues(self, patched_delete_issue):
        """Issues keyed by device name are removed."""
        hass = _make_hass()
        entry = _make_entry()

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        assert "invalid_external_temperature_Kinderzimmer" in called_ids
        assert "invalid_window_state_Kinderzimmer" in called_ids
        assert "degraded_mode_Kinderzimmer" in called_ids

    @pytest.mark.asyncio
    async def test_deletes_missing_entity_for_each_trv(self, patched_delete_issue):
        """``missing_entity_*`` issues are removed for every configured TRV."""
        hass = _make_hass()
        entry = _make_entry(
            **{
                CONF_HEATER: [
                    {"trv": "climate.trv_one", "advanced": {}},
                    {"trv": "climate.trv_two", "advanced": {}},
                ]
            }
        )

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        assert "missing_entity_climate.trv_one" in called_ids
        assert "missing_entity_climate.trv_two" in called_ids

    @pytest.mark.asyncio
    async def test_an_entry_whose_heater_is_a_bare_string_is_removed(
        self, patched_delete_issue
    ):
        """An entry migration refused, holding one entity id as a string, still goes.

        The removal reads the thermostats the same way the migration does, so
        the string is the one entity id, not a sequence of characters.
        """
        hass = _make_hass()
        entry = _make_entry(**{CONF_HEATER: "climate.trv_one"})

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        assert "missing_entity_climate.trv_one" in called_ids

    @pytest.mark.asyncio
    async def test_deletes_missing_entity_for_optional_sensors(
        self, patched_delete_issue
    ):
        """Optional sensors (when configured) also get their issues cleaned up."""
        hass = _make_hass()
        entry = _make_entry(
            **{
                CONF_HUMIDITY: "sensor.humidity",
                CONF_SENSOR_WINDOW: "binary_sensor.window",
                CONF_OUTDOOR_SENSOR: "sensor.outdoor",
            }
        )

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        assert "missing_entity_sensor.kinderzimmer_temperature" in called_ids
        assert "missing_entity_sensor.humidity" in called_ids
        assert "missing_entity_binary_sensor.window" in called_ids
        assert "missing_entity_sensor.outdoor" in called_ids

    @pytest.mark.asyncio
    async def test_deletes_every_issue_a_fully_wired_thermostat_can_raise(
        self, patched_delete_issue
    ):
        """No repair issue of a removed thermostat outlives it.

        The runtime raises one issue per contact role and one per watched
        entity; every device the entry wires is watched, the door sensor and
        the cooler included.
        """
        hass = _make_hass()
        entry = _make_entry(
            **{
                CONF_HUMIDITY: "sensor.humidity",
                CONF_SENSOR_WINDOW: "binary_sensor.window",
                CONF_SENSOR_DOOR: "binary_sensor.door",
                CONF_OUTDOOR_SENSOR: "sensor.outdoor",
                CONF_COOLER: "climate.ac",
            }
        )

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        raised_by_the_runtime = {
            "invalid_external_temperature_Kinderzimmer",
            f"{WINDOW.issue_translation_key}_Kinderzimmer",
            f"{DOOR.issue_translation_key}_Kinderzimmer",
            "degraded_mode_Kinderzimmer",
            "missing_entity_climate.fritz_kinderzimmer",
            "missing_entity_sensor.kinderzimmer_temperature",
            "missing_entity_sensor.humidity",
            "missing_entity_binary_sensor.window",
            "missing_entity_binary_sensor.door",
            "missing_entity_sensor.outdoor",
            "missing_entity_climate.ac",
        }
        assert raised_by_the_runtime - called_ids == set()

    @pytest.mark.asyncio
    async def test_skips_unconfigured_optional_sensors(self, patched_delete_issue):
        """Sensors not configured on the entry do not trigger spurious deletes."""
        hass = _make_hass()
        entry = _make_entry()

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        assert "missing_entity_sensor.kinderzimmer_temperature" in called_ids
        assert not any(
            cid.startswith("missing_entity_")
            and cid != "missing_entity_sensor.kinderzimmer_temperature"
            and "climate.fritz_kinderzimmer" not in cid
            for cid in called_ids
        )

    @pytest.mark.asyncio
    async def test_uses_domain_for_every_delete(self, patched_delete_issue):
        """Every cleanup call targets BT's domain."""
        hass = _make_hass()
        entry = _make_entry()

        await async_remove_entry(hass, entry)

        assert patched_delete_issue.call_args_list
        for call in patched_delete_issue.call_args_list:
            assert call.args[1] == DOMAIN

    @pytest.mark.asyncio
    async def test_drops_the_reload_lock_of_the_removed_entry(
        self, patched_delete_issue
    ):
        """The lock an entry reloads on is dropped with the entry.

        The locks outlive the per-entry data on purpose, so that a reload can
        hold one across the unload it drives. Removal is therefore the only
        point at which one can be let go.
        """
        hass = _make_hass()
        entry = _make_entry()
        hass.data[RELOAD_LOCKS] = {entry.entry_id: Lock(), "other_entry": Lock()}

        await async_remove_entry(hass, entry)

        assert list(hass.data[RELOAD_LOCKS]) == ["other_entry"]
