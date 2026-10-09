"""Tests for repair-issue cleanup on config-entry unload and removal.

Better Thermostat raises repair issues for runtime conditions (an invalid
sensor reading, a missing entity, degraded mode). Removing the config entry
must remove them, so that none lingers after the thermostat is gone, and
must leave the issues of every other entry standing.
"""

from asyncio import Lock
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_NAME
from homeassistant.helpers import issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.better_thermostat import (
    DOMAIN,
    RELOAD_LOCKS,
    async_remove_entry,
    async_unload_entry,
)
from custom_components.better_thermostat.events.contact import CONTACT_ROLES
from custom_components.better_thermostat.utils.const import (
    CONF_HUMIDITY_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTAT,
)
from custom_components.better_thermostat.utils.helpers import entry_issue_id

TRV_ENTITY_ID = "climate.fritz_kinderzimmer"

# Every issue the runtime raises for one entry: one per condition of the
# thermostat, and the missing-entity issue of each of its TRVs.
ENTRY_WIDE_TRANSLATION_KEYS = (
    "invalid_external_temperature",
    "degraded_mode",
    *(role.issue_translation_key for role in CONTACT_ROLES),
)


def _make_entry(hass, entry_id: str = "abcd1234", **overrides) -> MockConfigEntry:
    data: dict[str, object] = {
        CONF_NAME: "Kinderzimmer",
        CONF_THERMOSTAT: [{"trv": TRV_ENTITY_ID, "advanced": {}}],
        CONF_TEMPERATURE_SENSOR: "sensor.kinderzimmer_temperature",
    }
    data.update(overrides)
    entry = MockConfigEntry(
        domain=DOMAIN, entry_id=entry_id, title="Kinderzimmer", data=data
    )
    entry.add_to_hass(hass)
    return entry


def _raise_every_issue(hass, entry_id: str) -> set[str]:
    """Raise every issue the runtime can raise for ``entry_id``; return their ids."""
    issue_ids = {
        entry_issue_id(entry_id, key) for key in ENTRY_WIDE_TRANSLATION_KEYS
    } | {entry_issue_id(entry_id, "missing_entity", TRV_ENTITY_ID)}
    for issue_id in issue_ids:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="degraded_mode",
        )
    return issue_ids


def _open_issues(hass) -> set[str]:
    return {
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    }


@pytest.fixture(autouse=True)
def patched_remove_store():
    """Keep the state store out of the repair-issue assertions.

    Removing the store is a separate concern that reaches for the config
    directory, which these tests do not look at.
    """
    with patch(
        "custom_components.better_thermostat.utils.state_manager"
        ".StateManager.async_remove_store",
        new_callable=AsyncMock,
    ):
        yield


class TestAsyncRemoveEntryCleansRepairIssues:
    """``async_remove_entry`` deletes the repair issues of its entry and no others."""

    async def test_deletes_every_issue_the_entry_raised(self, hass):
        """No repair issue of a removed thermostat outlives it."""
        entry = _make_entry(hass)
        assert _raise_every_issue(hass, entry.entry_id) == _open_issues(hass)

        await async_remove_entry(hass, entry)

        assert _open_issues(hass) == set()

    async def test_keeps_the_issues_of_an_entry_with_the_same_name(self, hass):
        """Two thermostats of one name keep their issues apart.

        The issues are told apart by the config entry, not by the name the
        user gave both thermostats.
        """
        removed = _make_entry(hass, "removed_entry")
        kept = _make_entry(hass, "kept_entry")
        _raise_every_issue(hass, removed.entry_id)
        kept_issues = _raise_every_issue(hass, kept.entry_id)

        await async_remove_entry(hass, removed)

        assert _open_issues(hass) == kept_issues

    @pytest.mark.parametrize(
        "stored",
        [
            pytest.param({CONF_THERMOSTAT: "climate.trv_one"}, id="bare-string"),
            pytest.param(
                {
                    CONF_THERMOSTAT: 3,
                    CONF_HUMIDITY_SENSOR: ["sensor.not_an_entity_id"],
                    CONF_OUTDOOR_SENSOR: 5,
                },
                id="unparsable",
            ),
            pytest.param({CONF_NAME: None}, id="name-none"),
        ],
    )
    async def test_an_entry_with_settings_setup_refuses_is_cleaned_up(
        self, hass, stored
    ):
        """Settings setup refuses do not stop the removal's cleanup.

        The issues are found by the entry id, so nothing in the stored
        settings has to parse for them to go.
        """
        entry = _make_entry(hass, **stored)
        _raise_every_issue(hass, entry.entry_id)

        await async_remove_entry(hass, entry)

        assert _open_issues(hass) == set()

    async def test_drops_the_reload_lock_of_the_removed_entry(self, hass):
        """The lock an entry reloads on is dropped with the entry.

        The locks outlive the per-entry data on purpose, so that a reload can
        hold one across the unload it drives. Removal is therefore the only
        point at which one can be let go.
        """
        entry = _make_entry(hass)
        hass.data[RELOAD_LOCKS] = {entry.entry_id: Lock(), "other_entry": Lock()}

        await async_remove_entry(hass, entry)

        assert list(hass.data[RELOAD_LOCKS]) == ["other_entry"]


class TestAsyncUnloadEntryCleansRepairIssues:
    """``async_unload_entry`` deletes the entry's issues once it has unloaded."""

    @pytest.mark.parametrize(("unloaded", "left_open"), [(True, False), (False, True)])
    async def test_the_issues_go_with_the_unload(self, hass, unloaded, left_open):
        """An entry whose platforms stay loaded keeps reporting what it raised."""
        entry = _make_entry(hass)
        raised = _raise_every_issue(hass, entry.entry_id)

        with patch.object(
            hass.config_entries,
            "async_unload_platforms",
            AsyncMock(return_value=unloaded),
        ):
            assert await async_unload_entry(hass, entry) is unloaded

        assert _open_issues(hass) == (raised if left_open else set())
