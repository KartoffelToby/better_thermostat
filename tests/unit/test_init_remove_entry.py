"""Tests for repair-issue cleanup on config-entry removal.

Better Thermostat creates persistent ``issue_registry`` entries for various
runtime conditions (invalid sensor reading, missing entity, degraded mode).
Deleting the config entry must remove the associated repair issues so that
they do not linger after the BT instance is gone.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat import DOMAIN, async_remove_entry
from custom_components.better_thermostat.utils.const import (
    CONF_HEATER,
    CONF_HUMIDITY,
    CONF_OUTDOOR_SENSOR,
    CONF_SENSOR,
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
        hass = AsyncMock()
        entry = _make_entry()

        await async_remove_entry(hass, entry)

        called_ids = {call.args[2] for call in patched_delete_issue.call_args_list}
        assert "invalid_external_temperature_Kinderzimmer" in called_ids
        assert "invalid_window_state_Kinderzimmer" in called_ids
        assert "degraded_mode_Kinderzimmer" in called_ids

    @pytest.mark.asyncio
    async def test_deletes_missing_entity_for_each_trv(self, patched_delete_issue):
        """``missing_entity_*`` issues are removed for every configured TRV."""
        hass = AsyncMock()
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
    async def test_deletes_missing_entity_for_optional_sensors(
        self, patched_delete_issue
    ):
        """Optional sensors (when configured) also get their issues cleaned up."""
        hass = AsyncMock()
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
    async def test_skips_unconfigured_optional_sensors(self, patched_delete_issue):
        """Sensors not configured on the entry do not trigger spurious deletes."""
        hass = AsyncMock()
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
        hass = AsyncMock()
        entry = _make_entry()

        await async_remove_entry(hass, entry)

        assert patched_delete_issue.call_args_list
        for call in patched_delete_issue.call_args_list:
            assert call.args[1] == DOMAIN
