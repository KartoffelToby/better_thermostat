"""Tests for the config-entry migration to version 18.

Every ``CONF_HEATER`` entry stores the entity id of its TRV under the key
``"trv"``. The migration to version 18 must read that key, ask the device
registry for the model of every TRV and store the answer under ``"model"``,
so that entries created before the model was recorded, or with a stale one,
carry the model the device-specific quirks are keyed by.
"""

from unittest.mock import AsyncMock, MagicMock, call, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat import async_migrate_entry
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CONF_HEATER,
    CONF_PROTECT_OVERHEATING,
    CONF_SENSOR,
    CalibrationMode,
    CalibrationType,
)

DETECTED_MODEL = "TRVZB"


def _make_trv(entity_id, **extra):
    """Return one ``CONF_HEATER`` entry in the shape a config flow stores."""
    return {
        "trv": entity_id,
        "integration": "generic",
        "adapter": "generic",
        "advanced": {
            CONF_CALIBRATION: CalibrationType.TARGET_TEMP_BASED,
            CONF_CALIBRATION_MODE: CalibrationMode.DEFAULT,
            CONF_PROTECT_OVERHEATING: False,
        },
        **extra,
    }


def _make_entry(trvs):
    """Return a version-17 config entry whose ``data`` is a real mapping."""
    entry = MagicMock()
    entry.version = 17
    entry.entry_id = "abcd1234"
    entry.title = "Kinderzimmer"
    entry.data = {
        CONF_NAME: "Kinderzimmer",
        CONF_SENSOR: "sensor.kinderzimmer_temperature",
        CONF_HEATER: trvs,
    }
    return entry


def _make_hass():
    """Return a Home Assistant double that records the entry update.

    The migration only reaches ``hass`` through the model lookup, which is
    patched, and through ``config_entries.async_update_entry``, which is a
    plain mock so the written ``data`` and ``version`` can be asserted on.
    """
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


@pytest.fixture
def patched_get_device_model():
    """Answer every model lookup with ``DETECTED_MODEL`` and return the mock."""
    with patch(
        "custom_components.better_thermostat.get_device_model",
        new_callable=AsyncMock,
        return_value=DETECTED_MODEL,
    ) as mock_lookup:
        yield mock_lookup


class TestMigrationToVersion18:
    """The migration to version 18 refreshes the model of every TRV."""

    async def test_migration_to_version_18_refreshes_the_model_of_every_trv(
        self, patched_get_device_model
    ):
        """Entries without a model get the detected one, one lookup per TRV."""
        hass = _make_hass()
        entry = _make_entry(
            [_make_trv("climate.kinderzimmer"), _make_trv("climate.wohnzimmer")]
        )

        assert await async_migrate_entry(hass, entry) is True

        looked_up = [
            lookup.args[1] for lookup in patched_get_device_model.await_args_list
        ]
        assert looked_up == ["climate.kinderzimmer", "climate.wohnzimmer"]

        hass.config_entries.async_update_entry.assert_called_once()
        update = hass.config_entries.async_update_entry.call_args
        assert update == call(entry, data=update.kwargs["data"], version=18)
        written_trvs = update.kwargs["data"][CONF_HEATER]
        assert written_trvs[0]["trv"] == "climate.kinderzimmer"
        assert written_trvs[0]["model"] == DETECTED_MODEL
        assert written_trvs[1]["trv"] == "climate.wohnzimmer"
        assert written_trvs[1]["model"] == DETECTED_MODEL

    async def test_migration_to_version_18_replaces_a_stale_model(
        self, patched_get_device_model
    ):
        """A model recorded earlier is replaced by the detected one."""
        hass = _make_hass()
        entry = _make_entry(
            [
                _make_trv("climate.kinderzimmer", model="generic"),
                _make_trv("climate.wohnzimmer", model="generic"),
            ]
        )

        assert await async_migrate_entry(hass, entry) is True

        looked_up = [
            lookup.args[1] for lookup in patched_get_device_model.await_args_list
        ]
        assert looked_up == ["climate.kinderzimmer", "climate.wohnzimmer"]

        update = hass.config_entries.async_update_entry.call_args
        assert update.kwargs["version"] == 18
        written_trvs = update.kwargs["data"][CONF_HEATER]
        assert written_trvs[0]["model"] == DETECTED_MODEL
        assert written_trvs[1]["model"] == DETECTED_MODEL
