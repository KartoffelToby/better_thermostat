"""Tests for the one-time v0 store migration (migrate_v0_stores module).

Covers:
- _filter_by_prefix: key filtering by prefix with type guards
- _import_legacy_data: deserialization of MPC/PID/TPI/thermal data into StateManager
- migrate_v0_stores: full async migration flow with mocked HA Store files
  - Skip when unified store already has data
  - Import from all four legacy stores
  - Partial availability (some stores missing/empty/corrupt)
  - the state saved only when data was actually imported
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers import storage
from homeassistant.util.file import WriteError
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
    async_remove_stores,
)

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
        """Entries whose value is not a dict are excluded even if key matches."""
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

    @pytest.mark.parametrize(
        ("section", "entry"),
        [
            ("mpc", {"gain_est": "abc"}),
            ("mpc", {"gain_est": float("nan")}),
            ("pid", {"pid_kp": "abc"}),
            ("pid", {"pid_kp": float("nan")}),
            ("tpi", {"last_update_ts": "abc"}),
            ("tpi", {"last_update_ts": float("nan")}),
        ],
    )
    def test_a_legacy_value_that_is_dropped_names_its_key(
        self, caplog, section, entry
    ) -> None:
        """A legacy value the import cannot use is reported with the entry key."""
        mgr = _make_state_manager()

        with caplog.at_level(logging.WARNING):
            _import_legacy_data(mgr, **{f"{section}_data": {"uid1:trv_a": entry}})

        assert any(
            "uid1:trv_a" in record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), caplog.text

    def test_import_thermal_data(self) -> None:
        """Thermal data is deserialized and stored as ThermalStats."""
        mgr = _make_state_manager()
        thermal_data = {"heating_power": 1200.0, "heat_loss_rate": 0.03}

        _import_legacy_data(mgr, thermal_data=thermal_data)

        assert mgr.thermal.heating_power == pytest.approx(1200.0)
        assert mgr.thermal.heat_loss_rate == pytest.approx(0.03)

    @pytest.mark.parametrize(
        "unusable", [float("nan"), float("inf"), float("-inf"), "nan", "abc", [1.0]]
    )
    @pytest.mark.parametrize("field", ["heating_power", "heat_loss_rate"])
    def test_an_unusable_legacy_thermal_stat_is_imported_as_unset(
        self, caplog, field, unusable
    ) -> None:
        """A legacy thermal stat that is not a finite number counts as unlearned.

        The other stat and the rest of the import go through, and the
        dropped one is named.
        """
        mgr = _make_state_manager()
        thermal_data = {"heating_power": 800.0, "heat_loss_rate": 0.03}
        thermal_data[field] = unusable

        with caplog.at_level(logging.WARNING):
            _import_legacy_data(
                mgr, thermal_data=thermal_data, pid_data={"uid1:trv_a": {"pid_kp": 2.0}}
            )

        assert getattr(mgr.thermal, field) is None
        other = "heat_loss_rate" if field == "heating_power" else "heating_power"
        assert getattr(mgr.thermal, other) == pytest.approx(
            {"heating_power": 800.0, "heat_loss_rate": 0.03}[other]
        )
        assert mgr.get_pid("uid1:trv_a").pid_kp == pytest.approx(2.0)
        assert any(
            field in record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), caplog.text

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
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_pid_already_present(self) -> None:
        """Migration is skipped when the unified store already has PID data."""
        mgr = _make_state_manager()
        mgr.set_pid("existing_key", PIDState(pid_kp=2.0))
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_tpi_already_present(self) -> None:
        """Migration is skipped when the unified store already has TPI data."""
        mgr = _make_state_manager()
        mgr.set_tpi("existing_key", TpiState(last_percent=50.0))
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_thermal_already_present(self) -> None:
        """Migration is skipped when thermal stats already have values."""
        mgr = _make_state_manager()
        mgr.thermal = ThermalStats(heating_power=1000.0)
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_presets_already_present(self) -> None:
        """Migration is skipped when presets are already populated."""
        mgr = _make_state_manager()
        mgr.presets = {"comfort": 22.0}
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

        await migrate_v0_stores(
            AsyncMock(), mgr, entity_prefix="uid1:", config_entry_id="entry1"
        )

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_imports_all_four_stores(self) -> None:
        """All four legacy stores are read and their data imported."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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

        # the state saved exactly once
        mgr.save_unless_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_filters_by_entity_prefix(self) -> None:
        """Only entries matching the entity prefix are imported."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_data_for_entity_no_save(self) -> None:
        """When no legacy store has matching data, nothing is saved."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_stores_empty_no_save(self) -> None:
        """When all legacy stores return None, nothing is saved."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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

        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_load_exception_is_swallowed(self) -> None:
        """Exceptions during Store.async_load are caught and do not crash."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thermal_uses_config_entry_id(self) -> None:
        """Thermal store is keyed by config_entry_id, not entity prefix."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_thermal_non_dict_entry_ignored(self) -> None:
        """Non-dict thermal entry for the config entry is ignored."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_returns_non_dict_is_ignored(self) -> None:
        """If a Store returns a non-dict (e.g. list), that store is skipped."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_only_thermal_imported(self) -> None:
        """Migration works when only thermal data exists."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_stores_raise_no_crash(self) -> None:
        """If all four stores raise exceptions, migration completes without crash."""
        mgr = _make_state_manager()
        mgr.save_unless_closed = AsyncMock()  # type: ignore[method-assign]

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
        mgr.save_unless_closed.assert_not_called()


class TestTheMigrationSaveWaitsLikeEveryOtherSave:
    """The migration's save runs as the task the entity's final flush waits for.

    It runs inside the entity's startup, after the removal hook is in place,
    so the entity can be removed while it is under way. A store that load()
    could not read and could not set aside makes that save try the copy
    first; the flush has to see that try's outcome, and once the manager is
    closed the migration writes nothing of its own.
    """

    _ENTRY = "v0_entry"
    _LIVE_KEY = f"better_thermostat_{_ENTRY}_state"
    _COPY_KEY = f"better_thermostat_{_ENTRY}_state.corrupt"
    _UNREADABLE = {"version": "unreadable", "mpc": {}}
    _LEGACY_KEY = "better_thermostat_mpc_states"

    def _seed(self, hass_storage, *, live: dict | None) -> None:
        if live is not None:
            hass_storage[self._LIVE_KEY] = {
                "version": 1,
                "minor_version": 1,
                "key": self._LIVE_KEY,
                "data": live,
            }
        hass_storage[self._LEGACY_KEY] = {
            "version": 1,
            "minor_version": 1,
            "key": self._LEGACY_KEY,
            "data": {"uid1:trv_a:t22": {"gain_est": 0.5}},
        }

    async def _migrate(self, hass, manager: StateManager) -> None:
        await migrate_v0_stores(
            hass, manager, entity_prefix="uid1:", config_entry_id=self._ENTRY
        )

    async def test_a_startup_with_legacy_stores_saves_the_migrated_state(
        self, hass, hass_storage
    ):
        """On a normal startup the imported state reaches the live store."""
        self._seed(hass_storage, live=None)
        manager = StateManager(hass, self._ENTRY)
        await manager.load()

        await self._migrate(hass, manager)
        await hass.async_block_till_done(wait_background_tasks=True)

        saved = hass_storage[self._LIVE_KEY]["data"]
        assert saved["mpc"]["uid1:trv_a:t22"]["gain_est"] == pytest.approx(0.5)
        assert manager.dirty is False

    async def test_a_pending_copy_that_lands_lets_the_migration_save(
        self, hass, hass_storage
    ):
        """A copy that failed at load and succeeds now is taken, then the save."""
        self._seed(hass_storage, live=self._UNREADABLE)
        write = storage.Store._async_write_data
        disk = {"full": True}

        async def _write(store, data):
            if ".corrupt" in store.key and disk["full"]:
                raise WriteError("disk full")
            await write(store, data)

        with patch.object(storage.Store, "_async_write_data", _write):
            manager = StateManager(hass, self._ENTRY)
            await manager.load()
            assert self._COPY_KEY not in hass_storage
            disk["full"] = False

            await self._migrate(hass, manager)
            await hass.async_block_till_done(wait_background_tasks=True)
            manager.close()

        assert hass_storage[self._COPY_KEY]["data"] == self._UNREADABLE
        saved = hass_storage[self._LIVE_KEY]["data"]
        assert saved["mpc"]["uid1:trv_a:t22"]["gain_est"] == pytest.approx(0.5)
        assert manager.dirty is False

    async def test_a_removal_during_the_migration_copy_waits_and_leaves_no_store(
        self, hass, hass_storage
    ):
        """The flush waits for the migration's copy; nothing is written after it.

        A copy tried beside the migration's may fail where that one
        succeeds, and a migration that saved after the flush would recreate
        the store the removal deletes.
        """
        self._seed(hass_storage, live=self._UNREADABLE)
        write = storage.Store._async_write_data
        disk = {"full": True}
        entered = asyncio.Event()
        release = asyncio.Event()
        gated_writes = {"count": 0}

        async def _write(store, data):
            if ".corrupt" in store.key:
                if disk["full"]:
                    raise WriteError("disk full")
                gated_writes["count"] += 1
                if gated_writes["count"] > 1:
                    raise WriteError("disk busy")
                entered.set()
                await release.wait()
            await write(store, data)

        with patch.object(storage.Store, "_async_write_data", _write):
            manager = StateManager(hass, self._ENTRY)
            await manager.load()
            disk["full"] = False

            migration = hass.async_create_task(self._migrate(hass, manager))
            await asyncio.wait_for(entered.wait(), timeout=5)

            manager.close()
            flush = hass.async_create_task(manager.flush())
            for _ in range(20):
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(flush, timeout=5)
            flushed = hass_storage[self._LIVE_KEY]["data"]

            await async_remove_stores(hass, self._ENTRY)
            await asyncio.wait_for(migration, timeout=5)
            await hass.async_block_till_done(wait_background_tasks=True)

        assert flushed["mpc"]["uid1:trv_a:t22"]["gain_est"] == pytest.approx(0.5)
        assert not [key for key in hass_storage if key.startswith(self._LIVE_KEY)]

    async def test_a_manager_closed_before_the_save_writes_nothing(
        self, hass, hass_storage
    ):
        """A removal that ran while the legacy stores were read leaves no store.

        Its flush wrote what was imported by then; a save after it would
        recreate the store the removal deletes.
        """
        self._seed(hass_storage, live=None)
        manager = StateManager(hass, self._ENTRY)
        await manager.load()
        manager.close()

        await self._migrate(hass, manager)
        await hass.async_block_till_done(wait_background_tasks=True)

        assert self._LIVE_KEY not in hass_storage
        assert manager.dirty is True
