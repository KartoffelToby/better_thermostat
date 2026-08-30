"""Tests for the one-time v0 store migration (migrate_v0_stores module).

Covers:
- _filter_by_prefix: key filtering by prefix with type guards
- _import_legacy_data: deserialization of MPC/PID/TPI/thermal data into StateManager
- migrate_v0_stores: full async migration flow with mocked HA Store files
  - Skip when unified store already has data
  - Import from all four legacy stores
  - Partial availability (some stores missing/empty/corrupt)
  - save() called only when data was actually imported
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import MpcState
from custom_components.better_thermostat.utils.calibration.pid import PIDState
from custom_components.better_thermostat.utils.calibration.tpi import TpiState
from custom_components.better_thermostat.utils.migrate_v0_stores import (
    _filter_by_prefix,
    _import_legacy_data,
    migrate_v0_stores,
)
from custom_components.better_thermostat.utils.state_manager import (
    StateManager,
    ThermalStats,
)

_MIGRATE_LOGGER = "custom_components.better_thermostat.utils.migrate_v0_stores"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state_manager() -> StateManager:
    """Create a StateManager with a mocked Store (empty initial state)."""
    mock_hass = AsyncMock()
    with patch("custom_components.better_thermostat.utils.state_manager.Store"):
        return StateManager(mock_hass, "test_entry")


# ---------------------------------------------------------------------------
# _filter_by_prefix
# ---------------------------------------------------------------------------


class TestFilterByPrefix:
    """_filter_by_prefix returns entries whose key starts with prefix and value is dict."""

    def test_matching_entries_returned(self) -> None:
        """Keys that start with the prefix and have dict values are kept."""
        raw = {
            "uid1:trv_a": {"gain_est": 0.5},
            "uid1:trv_b": {"gain_est": 0.8},
            "uid2:trv_a": {"gain_est": 0.3},
        }
        result = _filter_by_prefix(raw, "uid1:")
        assert len(result) == 2
        assert "uid1:trv_a" in result
        assert "uid1:trv_b" in result
        assert "uid2:trv_a" not in result

    def test_non_dict_values_excluded(self) -> None:
        """Only entries carrying a controller state survive the prefix filter.

        The return type promises `dict[str, dict[str, Any]]`, and the three
        callers hand the result straight to the deserializers. Those check
        the value again before reading it, so a stray string costs nothing
        today; what it would cost is the promise, which is the only reason
        the callers may be written the way they are.
        """
        raw = {
            "uid1:trv_a": {"gain_est": 0.5},
            "uid1:trv_b": "not_a_dict",
            "uid1:trv_c": 42,
            "uid1:trv_d": [1, 2, 3],
            "uid1:trv_e": None,
        }
        result = _filter_by_prefix(raw, "uid1:")
        assert len(result) == 1
        assert "uid1:trv_a" in result

    def test_non_string_keys_excluded(self) -> None:
        """Non-string keys are excluded (defensive against corrupt data)."""
        raw = {
            "uid1:trv_a": {"gain_est": 0.5},
            42: {"gain_est": 0.8},  # type: ignore[dict-item]
        }
        result = _filter_by_prefix(raw, "uid1:")
        assert len(result) == 1

    def test_empty_dict_returns_empty(self) -> None:
        """Empty input returns empty result."""
        assert _filter_by_prefix({}, "uid1:") == {}

    def test_no_matching_prefix(self) -> None:
        """No keys match the prefix."""
        raw = {"uid2:trv_a": {"gain_est": 0.5}}
        assert _filter_by_prefix(raw, "uid1:") == {}

    def test_empty_prefix_matches_all_dicts(self) -> None:
        """Empty prefix matches all entries that have dict values."""
        raw = {"a": {"x": 1}, "b": {"y": 2}, "c": "not_dict"}
        result = _filter_by_prefix(raw, "")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _import_legacy_data
# ---------------------------------------------------------------------------


class TestImportLegacyData:
    """_import_legacy_data writes deserialized data into a StateManager."""

    def test_import_mpc_data(self) -> None:
        """MPC data is deserialized and stored via set_mpc."""
        mgr = _make_state_manager()
        mpc_data = {
            "uid1:trv_a:t22.0": {
                "gain_est": 0.5,
                "loss_est": 0.02,
                "dead_zone_hits": 3,
                "is_calibration_active": True,
                "trv_profile": "linear",
            }
        }

        _import_legacy_data(mgr, mpc_data=mpc_data)

        mpc = mgr.state.mpc["uid1:trv_a:t22.0"]
        assert isinstance(mpc, MpcState)
        assert mpc.gain_est == pytest.approx(0.5)
        assert mpc.loss_est == pytest.approx(0.02)
        assert mpc.dead_zone_hits == 3
        assert mpc.is_calibration_active is True
        assert mpc.trv_profile == "linear"

    def test_import_pid_data(self) -> None:
        """PID data is deserialized and stored via set_pid."""
        mgr = _make_state_manager()
        pid_data = {
            "uid1:trv_a": {
                "pid_integral": 1.5,
                "pid_kp": 2.0,
                "auto_tune": True,
                "last_delta_sign": -1,
            }
        }

        _import_legacy_data(mgr, pid_data=pid_data)

        pid = mgr.state.pid["uid1:trv_a"]
        assert isinstance(pid, PIDState)
        assert pid.pid_integral == pytest.approx(1.5)
        assert pid.pid_kp == pytest.approx(2.0)
        assert pid.auto_tune is True
        assert pid.last_delta_sign == -1

    def test_import_tpi_data(self) -> None:
        """TPI data is deserialized and stored via set_tpi."""
        mgr = _make_state_manager()
        tpi_data = {
            "uid1:trv_a:t22.0": {"last_percent": 65.0, "last_update_ts": 1700000000.0}
        }

        _import_legacy_data(mgr, tpi_data=tpi_data)

        tpi = mgr.state.tpi["uid1:trv_a:t22.0"]
        assert isinstance(tpi, TpiState)
        assert tpi.last_percent == pytest.approx(65.0)
        assert tpi.last_update_ts == pytest.approx(1700000000.0)

    def test_import_thermal_data(self) -> None:
        """Thermal data is deserialized and stored as ThermalStats."""
        mgr = _make_state_manager()
        thermal_data = {"heating_power": 1200.0, "heat_loss_rate": 0.03}

        _import_legacy_data(mgr, thermal_data=thermal_data)

        assert mgr.thermal.heating_power == pytest.approx(1200.0)
        assert mgr.thermal.heat_loss_rate == pytest.approx(0.03)

    def test_import_thermal_partial(self) -> None:
        """Thermal data with only one field leaves the other as None."""
        mgr = _make_state_manager()
        thermal_data = {"heating_power": 800.0}

        _import_legacy_data(mgr, thermal_data=thermal_data)

        assert mgr.thermal.heating_power == pytest.approx(800.0)
        assert mgr.thermal.heat_loss_rate is None

    def test_import_all_types_at_once(self) -> None:
        """All four data types can be imported in a single call."""
        mgr = _make_state_manager()
        _import_legacy_data(
            mgr,
            mpc_data={"k1": {"gain_est": 0.5}},
            pid_data={"k1": {"pid_kp": 2.0}},
            tpi_data={"k1": {"last_percent": 50.0}},
            thermal_data={"heating_power": 900.0},
        )

        assert mgr.state.mpc["k1"].gain_est == pytest.approx(0.5)
        assert mgr.state.pid["k1"].pid_kp == pytest.approx(2.0)
        assert mgr.state.tpi["k1"].last_percent == pytest.approx(50.0)
        assert mgr.thermal.heating_power == pytest.approx(900.0)

    def test_import_skips_non_dict_values(self) -> None:
        """Non-dict values in the data dicts are silently skipped."""
        mgr = _make_state_manager()
        mpc_data = {
            "good_key": {"gain_est": 0.5},
            "bad_key": "not_a_dict",  # type: ignore[dict-item]
        }

        _import_legacy_data(mgr, mpc_data=mpc_data)

        assert "good_key" in mgr.state.mpc
        assert "bad_key" not in mgr.state.mpc

    def test_import_none_args_noop(self) -> None:
        """Passing None for all data types leaves the state untouched."""
        mgr = _make_state_manager()
        _import_legacy_data(mgr)

        assert mgr.state.mpc == {}
        assert mgr.state.pid == {}
        assert mgr.state.tpi == {}
        assert mgr.thermal.heating_power is None

    def test_import_empty_dicts_noop(self) -> None:
        """Passing empty dicts leaves the state untouched."""
        mgr = _make_state_manager()
        _import_legacy_data(mgr, mpc_data={}, pid_data={}, tpi_data={}, thermal_data={})

        assert mgr.state.mpc == {}
        assert mgr.state.pid == {}
        assert mgr.state.tpi == {}
        # thermal_data={} is falsy, so ThermalStats stays default
        assert mgr.thermal.heating_power is None

    def test_import_multiple_keys_per_type(self) -> None:
        """Multiple keys per data type are all imported."""
        mgr = _make_state_manager()
        mpc_data = {
            "uid1:trv_a:t20": {"gain_est": 0.3},
            "uid1:trv_a:t22": {"gain_est": 0.5},
            "uid1:trv_b:t20": {"gain_est": 0.4},
        }

        _import_legacy_data(mgr, mpc_data=mpc_data)

        assert len(mgr.state.mpc) == 3
        assert mgr.state.mpc["uid1:trv_a:t20"].gain_est == pytest.approx(0.3)
        assert mgr.state.mpc["uid1:trv_a:t22"].gain_est == pytest.approx(0.5)
        assert mgr.state.mpc["uid1:trv_b:t20"].gain_est == pytest.approx(0.4)

    def test_thermal_non_dict_ignored(self) -> None:
        """Non-dict thermal_data is silently ignored."""
        mgr = _make_state_manager()
        _import_legacy_data(mgr, thermal_data="not_a_dict")  # type: ignore[arg-type]

        assert mgr.thermal == ThermalStats()


# ---------------------------------------------------------------------------
# migrate_v0_stores — full async flow
# ---------------------------------------------------------------------------


def _make_mock_store(load_return: object = None) -> MagicMock:
    """Create a mock HA Store with configurable async_load return."""
    store = MagicMock()
    store.async_load = AsyncMock(return_value=load_return)
    return store


class TestMigrateV0Stores:
    """Tests for the full async migrate_v0_stores function."""

    @pytest.mark.asyncio
    async def test_skips_when_mpc_already_present(self) -> None:
        """Migration is skipped when the unified store already has MPC data."""
        mgr = _make_state_manager()
        mgr.set_mpc("existing_key", MpcState(gain_est=1.0))
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_pid_already_present(self) -> None:
        """Migration is skipped when the unified store already has PID data."""
        mgr = _make_state_manager()
        mgr.set_pid("existing_key", PIDState(pid_kp=2.0))
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_tpi_already_present(self) -> None:
        """Migration is skipped when the unified store already has TPI data."""
        mgr = _make_state_manager()
        mgr.set_tpi("existing_key", TpiState(last_percent=50.0))
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_thermal_already_present(self) -> None:
        """Migration is skipped when thermal stats already have values."""
        mgr = _make_state_manager()
        mgr.thermal = ThermalStats(heating_power=1000.0)
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_imports_all_four_stores(self) -> None:
        """All four legacy stores are read and their data imported."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        mpc_store = _make_mock_store(
            {"uid1:trv_a:t22": {"gain_est": 0.5, "loss_est": 0.02}}
        )
        pid_store = _make_mock_store({"uid1:trv_a": {"pid_kp": 2.0}})
        tpi_store = _make_mock_store({"uid1:trv_a:t22": {"last_percent": 65.0}})
        thermal_store = _make_mock_store(
            {"entry1": {"heating_power": 1200.0, "heat_loss_rate": 0.03}}
        )

        stores = [mpc_store, pid_store, tpi_store, thermal_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        # MPC imported
        assert "uid1:trv_a:t22" in mgr.state.mpc
        assert mgr.state.mpc["uid1:trv_a:t22"].gain_est == pytest.approx(0.5)

        # PID imported
        assert "uid1:trv_a" in mgr.state.pid
        assert mgr.state.pid["uid1:trv_a"].pid_kp == pytest.approx(2.0)

        # TPI imported
        assert "uid1:trv_a:t22" in mgr.state.tpi
        assert mgr.state.tpi["uid1:trv_a:t22"].last_percent == pytest.approx(65.0)

        # Thermal imported
        assert mgr.thermal.heating_power == pytest.approx(1200.0)
        assert mgr.thermal.heat_loss_rate == pytest.approx(0.03)

        # save() called exactly once
        mgr.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_filters_by_entity_prefix(self) -> None:
        """Only entries matching the entity prefix are imported."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        mpc_store = _make_mock_store(
            {"uid1:trv_a:t22": {"gain_est": 0.5}, "uid2:trv_a:t22": {"gain_est": 0.9}}
        )
        empty_store = _make_mock_store(None)

        stores = [mpc_store, empty_store, empty_store, empty_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        assert "uid1:trv_a:t22" in mgr.state.mpc
        assert "uid2:trv_a:t22" not in mgr.state.mpc

    @pytest.mark.asyncio
    async def test_partial_stores_some_empty(self) -> None:
        """Migration succeeds when some legacy stores return None."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        mpc_store = _make_mock_store({"uid1:trv_a:t22": {"gain_est": 0.5}})
        empty_store = _make_mock_store(None)

        stores = [mpc_store, empty_store, empty_store, empty_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        assert "uid1:trv_a:t22" in mgr.state.mpc
        assert mgr.state.pid == {}
        assert mgr.state.tpi == {}
        mgr.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_data_for_entity_no_save(self) -> None:
        """When no legacy store has matching data, save() is not called."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        # Stores exist but contain only data for a different entity
        mpc_store = _make_mock_store({"uid2:trv_a:t22": {"gain_est": 0.5}})
        empty_store = _make_mock_store(None)

        stores = [mpc_store, empty_store, empty_store, empty_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_stores_empty_no_save(self) -> None:
        """When all legacy stores return None, save() is not called."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        empty_store = _make_mock_store(None)
        stores = [empty_store, empty_store, empty_store, empty_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_load_exception_is_swallowed(self) -> None:
        """Exceptions during Store.async_load are caught and do not crash."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        # MPC store raises, but PID store has valid data
        mpc_store = MagicMock()
        mpc_store.async_load = AsyncMock(side_effect=OSError("disk read error"))

        pid_store = _make_mock_store({"uid1:trv_a": {"pid_kp": 3.0}})
        empty_store = _make_mock_store(None)

        stores = [mpc_store, pid_store, empty_store, empty_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        # PID was still imported despite MPC store failure
        assert "uid1:trv_a" in mgr.state.pid
        assert mgr.state.pid["uid1:trv_a"].pid_kp == pytest.approx(3.0)
        mgr.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thermal_uses_config_entry_id(self) -> None:
        """Thermal store is keyed by config_entry_id, not entity prefix."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        thermal_store = _make_mock_store(
            {"entry1": {"heating_power": 900.0}, "entry2": {"heating_power": 1100.0}}
        )
        empty_store = _make_mock_store(None)

        stores = [empty_store, empty_store, empty_store, thermal_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        # Only entry1's thermal data is imported
        assert mgr.thermal.heating_power == pytest.approx(900.0)
        mgr.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thermal_non_dict_entry_ignored(self) -> None:
        """Non-dict thermal entry for the config entry is ignored."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        thermal_store = _make_mock_store({"entry1": "corrupted"})
        empty_store = _make_mock_store(None)

        stores = [empty_store, empty_store, empty_store, thermal_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        # Nothing imported, thermal stays at defaults
        assert mgr.thermal.heating_power is None
        mgr.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_returns_non_dict_is_ignored(self) -> None:
        """If a Store returns a non-dict (e.g. list), that store is skipped."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        # MPC store returns a list instead of a dict
        mpc_store = _make_mock_store([1, 2, 3])
        pid_store = _make_mock_store({"uid1:trv_a": {"pid_kp": 2.0}})
        empty_store = _make_mock_store(None)

        stores = [mpc_store, pid_store, empty_store, empty_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        # MPC skipped (non-dict), PID imported
        assert mgr.state.mpc == {}
        assert "uid1:trv_a" in mgr.state.pid
        mgr.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_only_thermal_imported(self) -> None:
        """Migration works when only thermal data exists."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        thermal_store = _make_mock_store(
            {"entry1": {"heating_power": 500.0, "heat_loss_rate": 0.01}}
        )
        empty_store = _make_mock_store(None)

        stores = [empty_store, empty_store, empty_store, thermal_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        assert mgr.thermal.heating_power == pytest.approx(500.0)
        assert mgr.thermal.heat_loss_rate == pytest.approx(0.01)
        mgr.save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_stores_raise_no_crash(self) -> None:
        """An unreadable legacy store must not stop the migration.

        The old stores are read once at startup. A user whose disk gave up
        on one of them needs a thermostat that comes up regardless, on
        defaults, rather than an integration that fails to load.
        """
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        failing_store = MagicMock()
        failing_store.async_load = AsyncMock(side_effect=OSError("boom"))

        stores = [failing_store, failing_store, failing_store, failing_store]
        store_iter = iter(stores)

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        # No data imported, no save
        mgr.save.assert_not_called()


class TestUnreadableLegacyStoreIsTraced:
    """A legacy store that cannot be read leaves a debug trace naming it."""

    @pytest.mark.asyncio
    async def test_every_failing_store_is_named(self, caplog) -> None:
        """All four store names and the config entry appear in the trace."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        failing_store = MagicMock()
        failing_store.async_load = AsyncMock(side_effect=OSError("boom"))
        store_iter = iter([failing_store] * 4)

        with (
            caplog.at_level(logging.DEBUG, logger=_MIGRATE_LOGGER),
            patch(
                "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
                side_effect=lambda _hass, _ver, _key: next(store_iter),
            ),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        for store_name in ("MPC", "PID", "TPI", "thermal"):
            assert f"legacy {store_name} store not imported" in caplog.text
        assert "entry1" in caplog.text
        assert all(record.exc_info for record in caplog.records)

    @pytest.mark.asyncio
    async def test_readable_stores_leave_no_trace(self, caplog) -> None:
        """Stores that read cleanly report nothing."""
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        stores = [
            _make_mock_store({"uid1:trv_a": {"gain_est": 0.5}}),
            _make_mock_store(None),
            _make_mock_store(None),
            _make_mock_store(None),
        ]
        store_iter = iter(stores)

        with (
            caplog.at_level(logging.DEBUG, logger=_MIGRATE_LOGGER),
            patch(
                "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
                side_effect=lambda _hass, _ver, _key: next(store_iter),
            ),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        assert "not imported" not in caplog.text


class TestUnusableLegacyThermalValues:
    """A legacy thermal value that is not a finite number costs only itself.

    Letting the parse error out aborts the thermal import before it can
    mark anything as imported, so the migration is never saved and runs
    again on every start -- while the value that could be read is dropped
    along with the one that could not.
    """

    def test_an_unparsable_statistic_does_not_take_its_neighbour(self, caplog) -> None:
        """The import keeps every statistic it can read and reports the rest.

        Both statistics arrive from the same legacy entry. Letting one that
        `float()` refuses end the import would drop the readable one with
        it, and the user would lose a heat-loss rate because a heating
        power was corrupt.
        """
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger=_MIGRATE_LOGGER):
            _import_legacy_data(
                mgr, thermal_data={"heating_power": "n/a", "heat_loss_rate": 0.01}
            )

        assert mgr.thermal.heating_power is None
        assert mgr.thermal.heat_loss_rate == pytest.approx(0.01)
        assert "heating_power" in caplog.text
        assert "not a finite number" in caplog.text

    def test_non_finite_value_is_imported_as_unset(self) -> None:
        """NaN and infinity are as unusable as a value float() refuses."""
        mgr = _make_state_manager()

        _import_legacy_data(
            mgr,
            thermal_data={"heating_power": float("nan"), "heat_loss_rate": "Infinity"},
        )

        assert mgr.thermal.heating_power is None
        assert mgr.thermal.heat_loss_rate is None

    def test_readable_values_are_not_reported(self, caplog) -> None:
        """Nothing is logged when both statistics parse."""
        mgr = _make_state_manager()

        with caplog.at_level(logging.WARNING, logger=_MIGRATE_LOGGER):
            _import_legacy_data(
                mgr, thermal_data={"heating_power": 1200.0, "heat_loss_rate": 0.03}
            )

        assert caplog.text == ""

    @pytest.mark.asyncio
    async def test_a_store_that_yielded_nothing_usable_is_still_saved(self) -> None:
        """Reaching a legacy entry counts as an import, whatever it parsed to.

        The write is what carries the migration's result forward. Making it
        depend on the values parsing would throw away the knowledge that the
        entry was seen at all.

        The write alone does not make the migration a one-off. The next
        start skips only when the unified state holds data, and an entry
        that parsed to nothing leaves it empty, so this case still runs the
        scan again.
        """
        mgr = _make_state_manager()
        mgr.save = AsyncMock()  # type: ignore[method-assign]

        thermal_store = _make_mock_store({"entry1": {"heating_power": "n/a"}})
        empty_store = _make_mock_store(None)
        store_iter = iter([empty_store, empty_store, empty_store, thermal_store])

        with patch(
            "custom_components.better_thermostat.utils.migrate_v0_stores.Store",
            side_effect=lambda _hass, _ver, _key: next(store_iter),
        ):
            await migrate_v0_stores(
                AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
            )

        mgr.save.assert_awaited_once()
