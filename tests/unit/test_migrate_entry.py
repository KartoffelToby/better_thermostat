"""Tests for the config-entry migration to version 18.

Every ``CONF_HEATER`` entry names its climate entity under the ``trv`` key.
The migration to version 18 must look the device model up for that entity,
store the result under ``model`` on every entry, whether the entry carried
no model before or a stale one, and persist the entry at version 18.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat import async_migrate_entry
from custom_components.better_thermostat.utils.const import CONF_HEATER, CONF_SENSOR

DETECTED_MODEL = "TRVZB"

KINDERZIMMER_ADVANCED = {
    "calibration": "local_calibration_based",
    "calibration_mode": "default",
    "no_off_system_mode": False,
    "heat_auto_swapped": False,
    "child_lock": False,
}

BADEZIMMER_ADVANCED = {
    "calibration": "target_temp_based",
    "calibration_mode": "mpc_calibration",
    "no_off_system_mode": True,
    "heat_auto_swapped": False,
    "child_lock": True,
}


def _make_entry(heaters):
    """Return a config-entry double at version 17 with a real ``data`` dict."""
    entry = MagicMock()
    entry.version = 17
    entry.entry_id = "abcd1234"
    entry.title = "Kinderzimmer"
    entry.data = {
        CONF_NAME: "Kinderzimmer",
        CONF_SENSOR: "sensor.kinderzimmer_temperature",
        CONF_HEATER: heaters,
    }
    return entry


def _make_hass():
    """Return a Home Assistant double that records the entry update."""
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


@pytest.fixture
def patched_get_device_model():
    """Patch ``get_device_model`` where the migration looks it up."""
    with patch(
        "custom_components.better_thermostat.get_device_model",
        new=AsyncMock(return_value=DETECTED_MODEL),
    ) as mock_get_device_model:
        yield mock_get_device_model


class TestMigrationToVersion18:
    """The version-18 step refreshes the model of every configured TRV."""

    @pytest.mark.asyncio
    async def test_migration_to_version_18_refreshes_the_model_of_every_trv(
        self, patched_get_device_model
    ):
        """Entries without a model get the detected one and version becomes 18."""
        hass = _make_hass()
        entry = _make_entry(
            [
                {
                    "trv": "climate.kinderzimmer",
                    "integration": "generic",
                    "adapter": "generic",
                    "advanced": dict(KINDERZIMMER_ADVANCED),
                },
                {
                    "trv": "climate.badezimmer",
                    "integration": "generic",
                    "adapter": "generic",
                    "advanced": dict(BADEZIMMER_ADVANCED),
                },
            ]
        )

        result = await async_migrate_entry(hass, entry)

        assert result is True

        assert patched_get_device_model.await_count == 2
        first_lookup, second_lookup = patched_get_device_model.await_args_list
        assert first_lookup.args[1] == "climate.kinderzimmer"
        assert second_lookup.args[1] == "climate.badezimmer"
        assert first_lookup.args[0].hass is hass
        assert first_lookup.args[0].device_name == "Kinderzimmer"

        hass.config_entries.async_update_entry.assert_called_once()
        update = hass.config_entries.async_update_entry.call_args
        assert update.args[0] is entry
        assert update.kwargs["version"] == 18
        migrated_heaters = update.kwargs["data"][CONF_HEATER]
        assert migrated_heaters[0] == {
            "trv": "climate.kinderzimmer",
            "integration": "generic",
            "adapter": "generic",
            "advanced": KINDERZIMMER_ADVANCED,
            "model": DETECTED_MODEL,
        }
        assert migrated_heaters[1] == {
            "trv": "climate.badezimmer",
            "integration": "generic",
            "adapter": "generic",
            "advanced": BADEZIMMER_ADVANCED,
            "model": DETECTED_MODEL,
        }

    @pytest.mark.asyncio
    async def test_migration_to_version_18_replaces_a_stale_model(
        self, patched_get_device_model
    ):
        """A model stored by an older version is replaced by the detected one."""
        hass = _make_hass()
        entry = _make_entry(
            [
                {
                    "trv": "climate.kinderzimmer",
                    "integration": "generic",
                    "adapter": "generic",
                    "advanced": dict(KINDERZIMMER_ADVANCED),
                    "model": "generic",
                },
                {
                    "trv": "climate.badezimmer",
                    "integration": "generic",
                    "adapter": "generic",
                    "advanced": dict(BADEZIMMER_ADVANCED),
                    "model": "generic",
                },
            ]
        )

        result = await async_migrate_entry(hass, entry)

        assert result is True

        assert patched_get_device_model.await_count == 2
        first_lookup, second_lookup = patched_get_device_model.await_args_list
        assert first_lookup.args[1] == "climate.kinderzimmer"
        assert second_lookup.args[1] == "climate.badezimmer"

        hass.config_entries.async_update_entry.assert_called_once()
        update = hass.config_entries.async_update_entry.call_args
        assert update.args[0] is entry
        assert update.kwargs["version"] == 18
        migrated_heaters = update.kwargs["data"][CONF_HEATER]
        assert migrated_heaters[0]["model"] == DETECTED_MODEL
        assert migrated_heaters[1]["model"] == DETECTED_MODEL
