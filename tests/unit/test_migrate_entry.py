"""Tests for the config-entry migration to version 18.2.

The migration ends with the settings in the entry's options and its data
empty, at minor version 2.

Every ``CONF_HEATER`` entry stores the entity id of its TRV under the key
``"trv"``. The migration to version 18 reads that key, asks the device
registry for the model of every TRV and stores an answer that identifies the
device under ``"model"``, so the device-specific quirks are keyed by the model
the TRV really is. An answer that identifies nothing leaves a model the entry
already carries alone, because the migration runs once.
"""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_NAME
import pytest

from custom_components.better_thermostat import async_migrate_entry
from custom_components.better_thermostat.utils.const import (
    CONF_CALIBRATION,
    CONF_CALIBRATION_MODE,
    CONF_HEATER,
    CONF_NO_SYSTEM_MODE_OFF,
    CONF_PROTECT_OVERHEATING,
    CONF_SENSOR,
    CONF_WINDOW_TIMEOUT,
    CONF_WINDOW_TIMEOUT_AFTER,
    GENERIC_MODEL,
    CalibrationMode,
    CalibrationType,
)

DETECTED_MODEL = "TRVZB"
STALE_MODEL = "SPZB0001"
ENTRY_NAME = "Kinderzimmer"
ROOM_SENSOR = "sensor.kinderzimmer_temperature"


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
    entry.title = ENTRY_NAME
    entry.data = {CONF_NAME: ENTRY_NAME, CONF_SENSOR: ROOM_SENSOR, CONF_HEATER: trvs}
    entry.options = {}
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
        # The lookup reads ``hass`` and ``device_name`` off whatever it is
        # handed, so the context has to carry both.
        context = patched_get_device_model.await_args_list[0].args[0]
        assert context.hass is hass
        assert context.device_name == ENTRY_NAME

        hass.config_entries.async_update_entry.assert_called_once()
        update = hass.config_entries.async_update_entry.call_args
        assert update.args == (entry,)
        assert update.kwargs["version"] == 18
        # The written mapping is the whole entry, not just its heaters.
        written = update.kwargs["options"]
        assert written[CONF_NAME] == ENTRY_NAME
        assert written[CONF_SENSOR] == ROOM_SENSOR
        written_trvs = written[CONF_HEATER]
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
                _make_trv("climate.kinderzimmer", model=STALE_MODEL),
                _make_trv("climate.wohnzimmer", model=GENERIC_MODEL),
            ]
        )

        assert await async_migrate_entry(hass, entry) is True

        looked_up = [
            lookup.args[1] for lookup in patched_get_device_model.await_args_list
        ]
        assert looked_up == ["climate.kinderzimmer", "climate.wohnzimmer"]

        update = hass.config_entries.async_update_entry.call_args
        assert update.kwargs["version"] == 18
        written = update.kwargs["options"]
        assert written[CONF_NAME] == ENTRY_NAME
        assert written[CONF_SENSOR] == ROOM_SENSOR
        written_trvs = written[CONF_HEATER]
        assert written_trvs[0]["model"] == DETECTED_MODEL
        assert written_trvs[1]["model"] == DETECTED_MODEL

    async def test_migration_to_version_18_keeps_a_model_the_registry_cannot_confirm(
        self, patched_get_device_model
    ):
        """A lookup that identifies nothing leaves a recorded model standing."""
        patched_get_device_model.return_value = GENERIC_MODEL
        hass = _make_hass()
        entry = _make_entry([_make_trv("climate.kinderzimmer", model=STALE_MODEL)])

        assert await async_migrate_entry(hass, entry) is True

        assert patched_get_device_model.await_count == 1
        update = hass.config_entries.async_update_entry.call_args
        assert update.kwargs["version"] == 18
        written_trvs = update.kwargs["options"][CONF_HEATER]
        assert written_trvs[0]["model"] == STALE_MODEL

    async def test_migration_to_version_18_records_the_fallback_without_a_model(
        self, patched_get_device_model
    ):
        """A TRV the entry knows no model for takes the fallback answer."""
        patched_get_device_model.return_value = GENERIC_MODEL
        hass = _make_hass()
        entry = _make_entry([_make_trv("climate.kinderzimmer")])

        assert await async_migrate_entry(hass, entry) is True

        update = hass.config_entries.async_update_entry.call_args
        written_trvs = update.kwargs["options"][CONF_HEATER]
        assert written_trvs[0]["model"] == GENERIC_MODEL


def _make_legacy_entry(version, trvs, **top_level):
    """Return an entry at ``version`` with the keys an entry of that age carries."""
    entry = _make_entry(trvs)
    entry.version = version
    entry.data = {**entry.data, **top_level}
    return entry


def _legacy_trv(entity_id, **advanced):
    """Return a ``CONF_HEATER`` bundle as the early versions stored it."""
    return {"trv": entity_id, "advanced": {CONF_CALIBRATION: 0, **advanced}}


class TestMigrationChain:
    """An entry of any older version passes every step written after it."""

    @pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
    async def test_every_later_step_reaches_an_old_entry(
        self, version, patched_get_device_model
    ):
        """The entry ends up with every key the steps from its version on add."""
        stored_delay = {} if version <= 2 else {CONF_WINDOW_TIMEOUT: 30}
        hass = _make_hass()
        entry = _make_legacy_entry(
            version, [_legacy_trv("climate.kinderzimmer")], **stored_delay
        )

        assert await async_migrate_entry(hass, entry) is True

        update = hass.config_entries.async_update_entry.call_args
        assert update.kwargs["version"] == 18
        written = update.kwargs["options"]
        advanced = written[CONF_HEATER][0]["advanced"]
        expected_delay = 0 if version <= 2 else 30
        assert written[CONF_WINDOW_TIMEOUT] == expected_delay
        assert written[CONF_WINDOW_TIMEOUT_AFTER] == expected_delay
        if version <= 3:
            assert advanced[CONF_CALIBRATION_MODE] == CalibrationMode.MPC_CALIBRATION
        else:
            assert CONF_CALIBRATION_MODE not in advanced
        if version <= 4:
            assert advanced[CONF_NO_SYSTEM_MODE_OFF] is False
        else:
            assert CONF_NO_SYSTEM_MODE_OFF not in advanced
        assert written[CONF_HEATER][0]["model"] == DETECTED_MODEL

    @pytest.mark.parametrize("version", [2, 3])
    async def test_fixed_calibration_becomes_the_aggressive_mode(
        self, version, patched_get_device_model
    ):
        """A TRV set to fixed calibration keeps it as the aggressive mode."""
        stored_delay = {} if version <= 2 else {CONF_WINDOW_TIMEOUT: 0}
        hass = _make_hass()
        entry = _make_legacy_entry(
            version,
            [
                _legacy_trv(
                    "climate.kinderzimmer",
                    **{CalibrationMode.AGGRESIVE_CALIBRATION: True},
                )
            ],
            **stored_delay,
        )

        assert await async_migrate_entry(hass, entry) is True

        written = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        advanced = written[CONF_HEATER][0]["advanced"]
        assert advanced[CONF_CALIBRATION_MODE] == CalibrationMode.AGGRESIVE_CALIBRATION

    @pytest.mark.parametrize("version", [1, 5, 17])
    async def test_the_stored_entry_stays_untouched_until_it_is_written(
        self, version, patched_get_device_model
    ):
        """The migration builds a new mapping; the entry changes only on update."""
        hass = _make_hass()
        entry = _make_legacy_entry(
            version, [_legacy_trv("climate.kinderzimmer")], **{CONF_WINDOW_TIMEOUT: 0}
        )
        before = copy.deepcopy(dict(entry.data))

        assert await async_migrate_entry(hass, entry) is True

        assert dict(entry.data) == before

    @pytest.mark.parametrize("version", [1, 5, 17])
    async def test_an_entry_from_before_the_device_bundles_asks_to_be_re_added(
        self, version, patched_get_device_model, caplog
    ):
        """A thermostat stored as a bare entity id fails with the re-add hint."""
        hass = _make_hass()
        entry = _make_legacy_entry(
            version, [], **{CONF_WINDOW_TIMEOUT: 0, CONF_HEATER: "climate.a"}
        )
        before = copy.deepcopy(dict(entry.data))

        assert await async_migrate_entry(hass, entry) is False

        hass.config_entries.async_update_entry.assert_not_called()
        assert dict(entry.data) == before
        errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert ENTRY_NAME in errors[0]
        assert "add it again" in errors[0]


async def test_the_migration_leaves_the_settings_in_the_options(
    patched_get_device_model,
):
    """The data is emptied, and a setting already in the options is kept."""
    hass = _make_hass()
    entry = _make_entry([_make_trv("climate.kinderzimmer")])
    entry.version = 18
    entry.options = {CONF_SENSOR: "sensor.newer_sensor"}

    assert await async_migrate_entry(hass, entry) is True

    update = hass.config_entries.async_update_entry.call_args
    assert update.kwargs["data"] == {}
    assert update.kwargs["minor_version"] == 2
    assert update.kwargs["options"][CONF_NAME] == ENTRY_NAME
    assert update.kwargs["options"][CONF_SENSOR] == "sensor.newer_sensor"
