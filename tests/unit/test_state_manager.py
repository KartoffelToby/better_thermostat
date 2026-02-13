"""Tests for the unified StateManager and its serialization layer.

Covers:
- Dataclass defaults and field types
- Serialization roundtrip (_serialize / _deserialize)
- Type coercion during deserialization (int, bool, str, float)
- Graceful handling of missing, extra, and invalid fields
- Migration from v0 (unversioned) to v1
- StateManager dirty tracking
- StateManager get-or-create semantics
- StateManager load / save / flush lifecycle
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.better_thermostat.utils.state_manager import (
    CURRENT_VERSION,
    MpcState,
    PIDState,
    RuntimeState,
    StateManager,
    ThermalStats,
    TpiState,
    _deserialize,
    deserialize_mpc,
    deserialize_pid,
    deserialize_tpi,
    _migrate_v0_to_v1,
    _serialize,
)

# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------


class TestMpcStateDefaults:
    """MpcState should initialize with sensible defaults."""

    def test_numeric_defaults(self):
        """Nullable floats default to None, counters to 0, kalman_P to 1."""
        s = MpcState()
        assert s.last_percent is None
        assert s.last_update_ts == 0.0
        assert s.dead_zone_hits == 0
        assert s.kalman_P == 1.0

    def test_bool_defaults(self):
        """All boolean fields default to False."""
        s = MpcState()
        assert s.is_calibration_active is False
        assert s.regime_boost_active is False
        assert s.tolerance_hold_active is False

    def test_str_defaults(self):
        """trv_profile defaults to 'unknown'."""
        s = MpcState()
        assert s.trv_profile == "unknown"

    def test_collection_defaults(self):
        """Mutable collection fields default to empty."""
        s = MpcState()
        assert s.perf_curve == {}
        assert len(s.recent_errors) == 0

    def test_collection_defaults_are_independent(self):
        """Each instance should get its own mutable collections."""
        a = MpcState()
        b = MpcState()
        a.recent_errors.append(1.0)
        assert len(b.recent_errors) == 0


class TestPIDStateDefaults:
    """PIDState should initialize with sensible defaults."""

    def test_defaults(self):
        """Numeric fields default to 0.0, nullable fields to None."""
        s = PIDState()
        assert s.pid_integral == 0.0
        assert s.pid_last_meas is None
        assert s.auto_tune is None
        assert s.last_delta_sign is None


class TestTpiStateDefaults:
    """TpiState should initialize with sensible defaults."""

    def test_defaults(self):
        """last_percent is None, last_update_ts is 0.0."""
        s = TpiState()
        assert s.last_percent is None
        assert s.last_update_ts == 0.0


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


class TestSerializeDeserializeRoundtrip:
    """_serialize then _deserialize should produce equivalent state."""

    def test_empty_state_roundtrip(self):
        """Fresh RuntimeState survives a serialize/deserialize cycle."""
        original = RuntimeState()
        raw = _serialize(original)
        restored = _deserialize(raw)
        assert asdict(restored) == asdict(original)

    def test_mpc_roundtrip(self):
        """MPC state with various field types survives roundtrip."""
        original = RuntimeState()
        mpc = MpcState(
            last_percent=42.5,
            dead_zone_hits=3,
            is_calibration_active=True,
            trv_profile="linear",
            recent_errors=deque([0.1, -0.2, 0.05], maxlen=20),
            perf_curve={"20.0": {"gain": 1.5, "count": 10}},
        )
        original.mpc["trv1__20"] = mpc

        raw = _serialize(original)
        restored = _deserialize(raw)

        r_mpc = restored.mpc["trv1__20"]
        assert r_mpc.last_percent == 42.5
        assert r_mpc.dead_zone_hits == 3
        assert r_mpc.is_calibration_active is True
        assert r_mpc.trv_profile == "linear"
        assert list(r_mpc.recent_errors) == [0.1, -0.2, 0.05]
        assert r_mpc.perf_curve == {"20.0": {"gain": 1.5, "count": 10}}

    def test_pid_roundtrip(self):
        """PID state with int, bool, and float fields survives roundtrip."""
        original = RuntimeState()
        pid = PIDState(pid_integral=1.5, auto_tune=True, last_delta_sign=-1)
        original.pid["trv1"] = pid

        raw = _serialize(original)
        restored = _deserialize(raw)

        r_pid = restored.pid["trv1"]
        assert r_pid.pid_integral == 1.5
        assert r_pid.auto_tune is True
        assert r_pid.last_delta_sign == -1

    def test_tpi_roundtrip(self):
        """TPI state survives roundtrip."""
        original = RuntimeState()
        original.tpi["trv1"] = TpiState(last_percent=65.0, last_update_ts=1000.0)

        raw = _serialize(original)
        restored = _deserialize(raw)

        r_tpi = restored.tpi["trv1"]
        assert r_tpi.last_percent == 65.0
        assert r_tpi.last_update_ts == 1000.0

    def test_thermal_roundtrip(self):
        """ThermalStats survive roundtrip."""
        original = RuntimeState(
            thermal=ThermalStats(heating_power=1200.0, heat_loss_rate=0.03)
        )

        raw = _serialize(original)
        restored = _deserialize(raw)

        assert restored.thermal.heating_power == 1200.0
        assert restored.thermal.heat_loss_rate == 0.03

    def test_presets_roundtrip(self):
        """Preset temperatures survive roundtrip."""
        original = RuntimeState(presets={"comfort": 22.0, "eco": 18.5})

        raw = _serialize(original)
        restored = _deserialize(raw)

        assert restored.presets == {"comfort": 22.0, "eco": 18.5}

    def test_full_state_roundtrip(self):
        """Complete state with all sections populated."""
        original = RuntimeState(
            mpc={"k1": MpcState(gain_est=0.5, loss_est=0.02)},
            pid={"k1": PIDState(pid_kp=2.0)},
            tpi={"k1": TpiState(last_percent=30.0)},
            thermal=ThermalStats(heating_power=800.0),
            presets={"away": 16.0},
        )

        raw = _serialize(original)
        restored = _deserialize(raw)

        assert restored.mpc["k1"].gain_est == 0.5
        assert restored.pid["k1"].pid_kp == 2.0
        assert restored.tpi["k1"].last_percent == 30.0
        assert restored.thermal.heating_power == 800.0
        assert restored.presets["away"] == 16.0


# ---------------------------------------------------------------------------
# Type coercion during deserialization
# ---------------------------------------------------------------------------


class TestDeserializeMpcTypeCoercion:
    """deserialize_mpc should coerce types correctly."""

    def test_int_field_from_float(self):
        """Float values in int fields are truncated to int."""
        raw = {"dead_zone_hits": 3.0, "loss_learn_count": 5.7}
        mpc = deserialize_mpc(raw)
        assert mpc.dead_zone_hits == 3
        assert isinstance(mpc.dead_zone_hits, int)
        assert mpc.loss_learn_count == 5
        assert isinstance(mpc.loss_learn_count, int)

    def test_bool_field_from_int(self):
        """Integer values in bool fields are coerced to bool."""
        raw = {"is_calibration_active": 1, "regime_boost_active": 0}
        mpc = deserialize_mpc(raw)
        assert mpc.is_calibration_active is True
        assert mpc.regime_boost_active is False

    def test_str_field_from_number(self):
        """Numeric values in str fields are coerced to str."""
        raw = {"trv_profile": 123}
        mpc = deserialize_mpc(raw)
        assert mpc.trv_profile == "123"
        assert isinstance(mpc.trv_profile, str)

    def test_float_field_from_int(self):
        """Integer values in float fields are coerced to float."""
        raw = {"last_percent": 50, "kalman_P": 2}
        mpc = deserialize_mpc(raw)
        assert mpc.last_percent == 50.0
        assert isinstance(mpc.last_percent, float)

    def test_none_preserved(self):
        """None values are preserved for nullable fields."""
        raw = {"gain_est": None, "loss_est": None}
        mpc = deserialize_mpc(raw)
        assert mpc.gain_est is None
        assert mpc.loss_est is None

    def test_invalid_value_skipped(self):
        """Non-numeric strings and wrong types fall back to defaults."""
        raw = {"last_percent": "not_a_number", "gain_est": [1, 2]}
        mpc = deserialize_mpc(raw)
        assert mpc.last_percent is None  # default
        assert mpc.gain_est is None  # default

    def test_extra_fields_ignored(self):
        """Unknown fields in the raw dict are silently ignored."""
        raw = {"nonexistent_field": 42, "last_percent": 10.0}
        mpc = deserialize_mpc(raw)
        assert mpc.last_percent == 10.0
        assert not hasattr(mpc, "nonexistent_field")

    def test_empty_dict(self):
        """Empty dict produces a default MpcState."""
        mpc = deserialize_mpc({})
        assert mpc == MpcState()


class TestDeserializePidTypeCoercion:
    """deserialize_pid should coerce types correctly."""

    def test_int_field_from_float(self):
        """Float values in int fields are truncated to int."""
        raw = {"last_delta_sign": -1.0, "last_error_sign": 1.9}
        pid = deserialize_pid(raw)
        assert pid.last_delta_sign == -1
        assert pid.last_error_sign == 1

    def test_bool_field(self):
        """Integer value in auto_tune is coerced to bool."""
        raw = {"auto_tune": 1}
        pid = deserialize_pid(raw)
        assert pid.auto_tune is True

    def test_none_preserved(self):
        """None values are preserved for nullable fields."""
        raw = {"pid_kp": None}
        pid = deserialize_pid(raw)
        assert pid.pid_kp is None


class TestDeserializeTpi:
    """deserialize_tpi should coerce all fields to float."""

    def test_basic(self):
        """Integer values are coerced to float."""
        raw = {"last_percent": 80, "last_update_ts": 12345}
        tpi = deserialize_tpi(raw)
        assert tpi.last_percent == 80.0
        assert tpi.last_update_ts == 12345.0

    def test_invalid_skipped(self):
        """Non-numeric values fall back to defaults."""
        raw = {"last_percent": "bad"}
        tpi = deserialize_tpi(raw)
        assert tpi.last_percent is None


# ---------------------------------------------------------------------------
# Deserialization edge cases
# ---------------------------------------------------------------------------


class TestDeserializeEdgeCases:
    """Edge cases in full _deserialize function."""

    def test_missing_sections(self):
        """Missing top-level sections produce empty collections."""
        raw = {"version": 1}
        state = _deserialize(raw)
        assert state.mpc == {}
        assert state.pid == {}
        assert state.tpi == {}
        assert state.presets == {}

    def test_non_dict_mpc_payload_skipped(self):
        """Non-dict payloads inside mpc section are skipped."""
        raw = {"version": 1, "mpc": {"key1": "not_a_dict", "key2": 42}}
        state = _deserialize(raw)
        assert "key1" not in state.mpc
        assert "key2" not in state.mpc

    def test_non_dict_thermal_ignored(self):
        """Non-dict thermal section falls through to defaults."""
        raw = {"version": 1, "thermal": "garbage"}
        state = _deserialize(raw)
        assert state.thermal.heating_power is None

    def test_invalid_preset_skipped(self):
        """Non-numeric preset values are skipped, valid ones kept."""
        raw = {"version": 1, "presets": {"good": 21.0, "bad": "not_a_number"}}
        state = _deserialize(raw)
        assert state.presets["good"] == 21.0
        assert "bad" not in state.presets

    def test_non_dict_presets_ignored(self):
        """Non-dict presets section produces empty dict."""
        raw = {"version": 1, "presets": [1, 2, 3]}
        state = _deserialize(raw)
        assert state.presets == {}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigrationV0ToV1:
    """v0 to v1 migration adds missing top-level keys."""

    def test_adds_missing_keys(self):
        """Empty dict gets all required v1 keys."""
        raw: dict = {}
        result = _migrate_v0_to_v1(raw)
        assert result["version"] == 1
        assert result["mpc"] == {}
        assert result["pid"] == {}
        assert result["tpi"] == {}
        assert result["thermal"] == {}
        assert result["presets"] == {}

    def test_preserves_existing_data(self):
        """Existing data is preserved during migration."""
        raw = {"mpc": {"k": {"gain_est": 0.5}}, "thermal": {"heating_power": 1000}}
        result = _migrate_v0_to_v1(raw)
        assert result["version"] == 1
        assert result["mpc"]["k"]["gain_est"] == 0.5
        assert result["thermal"]["heating_power"] == 1000

    def test_does_not_overwrite_existing_version(self):
        """Setdefault does not overwrite an existing version key."""
        raw = {"version": 99}
        result = _migrate_v0_to_v1(raw)
        assert result["version"] == 99


# ---------------------------------------------------------------------------
# StateManager — dirty tracking
# ---------------------------------------------------------------------------


class TestStateManagerDirtyTracking:
    """Dirty flag tracks whether unsaved changes exist."""

    def _make_manager(self) -> StateManager:
        """Create a StateManager with a mocked Store."""
        mock_hass = AsyncMock()
        with patch("custom_components.better_thermostat.utils.state_manager.Store"):
            return StateManager(mock_hass, "test_entry")

    def test_starts_clean(self):
        """Fresh StateManager is not dirty."""
        mgr = self._make_manager()
        assert mgr.dirty is False

    def test_mark_dirty(self):
        """mark_dirty() sets the dirty flag."""
        mgr = self._make_manager()
        mgr.mark_dirty()
        assert mgr.dirty is True

    def test_get_mpc_creates_and_dirties(self):
        """get_mpc for a new key creates state and sets dirty."""
        mgr = self._make_manager()
        mpc = mgr.get_mpc("key1")
        assert isinstance(mpc, MpcState)
        assert mgr.dirty is True

    def test_get_mpc_existing_not_dirty(self):
        """get_mpc for an existing key does not set dirty."""
        mgr = self._make_manager()
        mgr.get_mpc("key1")
        mgr._dirty = False  # Reset
        mpc2 = mgr.get_mpc("key1")
        assert mgr.dirty is False
        assert isinstance(mpc2, MpcState)

    def test_set_mpc_dirties(self):
        """set_mpc always sets dirty."""
        mgr = self._make_manager()
        mgr.set_mpc("key1", MpcState(gain_est=1.0))
        assert mgr.dirty is True
        assert mgr.get_mpc("key1").gain_est == 1.0

    def test_get_pid_creates_and_dirties(self):
        """get_pid for a new key creates state and sets dirty."""
        mgr = self._make_manager()
        pid = mgr.get_pid("key1")
        assert isinstance(pid, PIDState)
        assert mgr.dirty is True

    def test_set_pid_dirties(self):
        """set_pid always sets dirty."""
        mgr = self._make_manager()
        mgr.set_pid("key1", PIDState(pid_kp=3.0))
        assert mgr.dirty is True

    def test_get_tpi_creates_and_dirties(self):
        """get_tpi for a new key creates state and sets dirty."""
        mgr = self._make_manager()
        tpi = mgr.get_tpi("key1")
        assert isinstance(tpi, TpiState)
        assert mgr.dirty is True

    def test_set_tpi_dirties(self):
        """set_tpi always sets dirty."""
        mgr = self._make_manager()
        mgr.set_tpi("key1", TpiState(last_percent=50.0))
        assert mgr.dirty is True

    def test_thermal_setter_dirties(self):
        """Assigning thermal property sets dirty."""
        mgr = self._make_manager()
        mgr.thermal = ThermalStats(heating_power=500.0)
        assert mgr.dirty is True
        assert mgr.thermal.heating_power == 500.0

    def test_presets_setter_dirties(self):
        """Assigning presets property sets dirty."""
        mgr = self._make_manager()
        mgr.presets = {"eco": 18.0}
        assert mgr.dirty is True
        assert mgr.presets == {"eco": 18.0}


# ---------------------------------------------------------------------------
# StateManager — load / save lifecycle
# ---------------------------------------------------------------------------


class TestStateManagerLoadSave:
    """Load, save, save_if_dirty, and flush lifecycle."""

    def _make_manager_with_store(self):
        """Create a StateManager with a capturable mock Store."""
        mock_hass = AsyncMock()
        mock_store = AsyncMock()
        with patch(
            "custom_components.better_thermostat.utils.state_manager.Store",
            return_value=mock_store,
        ):
            mgr = StateManager(mock_hass, "test_entry")
        return mgr, mock_store

    @pytest.mark.asyncio
    async def test_load_empty_store(self):
        """Loading from an empty store keeps default state."""
        mgr, mock_store = self._make_manager_with_store()
        mock_store.async_load.return_value = None

        await mgr.load()

        assert mgr.state.mpc == {}
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_load_valid_state(self):
        """Loading valid v1 data populates all sections."""
        mgr, mock_store = self._make_manager_with_store()
        mock_store.async_load.return_value = {
            "version": 1,
            "mpc": {"k1": {"gain_est": 0.5, "dead_zone_hits": 2}},
            "pid": {},
            "tpi": {},
            "thermal": {"heating_power": 1000.0},
            "presets": {"comfort": 22.0},
        }

        await mgr.load()

        assert mgr.state.mpc["k1"].gain_est == 0.5
        assert mgr.state.mpc["k1"].dead_zone_hits == 2
        assert mgr.state.thermal.heating_power == 1000.0
        assert mgr.state.presets["comfort"] == 22.0
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_load_triggers_migration(self):
        """Loading v0 data (no version key) triggers migration to v1."""
        mgr, mock_store = self._make_manager_with_store()
        mock_store.async_load.return_value = {"mpc": {"k1": {"gain_est": 0.3}}}

        await mgr.load()

        assert mgr.state.version == 1
        assert mgr.state.mpc["k1"].gain_est == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_save_writes_to_store(self):
        """save() serializes state and calls async_save on the Store."""
        mgr, mock_store = self._make_manager_with_store()
        mgr.set_mpc("k1", MpcState(last_percent=75.0))

        await mgr.save()

        mock_store.async_save.assert_called_once()
        saved_data = mock_store.async_save.call_args[0][0]
        assert saved_data["version"] == CURRENT_VERSION
        assert saved_data["mpc"]["k1"]["last_percent"] == 75.0
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_save_if_dirty_skips_when_clean(self):
        """save_if_dirty() does nothing when state is clean."""
        mgr, mock_store = self._make_manager_with_store()
        assert mgr.dirty is False

        await mgr.save_if_dirty()

        mock_store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_save_if_dirty_saves_when_dirty(self):
        """save_if_dirty() saves when dirty flag is set."""
        mgr, mock_store = self._make_manager_with_store()
        mgr.mark_dirty()

        await mgr.save_if_dirty()

        mock_store.async_save.assert_called_once()
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_flush_delegates_to_save_if_dirty(self):
        """flush() saves dirty state."""
        mgr, mock_store = self._make_manager_with_store()
        mgr.set_mpc("k1", MpcState())

        await mgr.flush()

        mock_store.async_save.assert_called_once()
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_flush_noop_when_clean(self):
        """flush() does nothing when state is clean."""
        mgr, mock_store = self._make_manager_with_store()

        await mgr.flush()

        mock_store.async_save.assert_not_called()


# ---------------------------------------------------------------------------
# StateManager — state property
# ---------------------------------------------------------------------------


class TestStateManagerStateAccess:
    """Public property access on StateManager."""

    def _make_manager(self) -> StateManager:
        """Create a StateManager with a mocked Store."""
        mock_hass = AsyncMock()
        with patch("custom_components.better_thermostat.utils.state_manager.Store"):
            return StateManager(mock_hass, "test_entry")

    def test_state_returns_runtime_state(self):
        """State property returns a RuntimeState with current version."""
        mgr = self._make_manager()
        assert isinstance(mgr.state, RuntimeState)
        assert mgr.state.version == CURRENT_VERSION

    def test_thermal_getter(self):
        """Thermal property returns ThermalStats."""
        mgr = self._make_manager()
        assert isinstance(mgr.thermal, ThermalStats)

    def test_presets_getter(self):
        """Presets property returns empty dict by default."""
        mgr = self._make_manager()
        assert mgr.presets == {}

    def test_multiple_keys_independent(self):
        """Different MPC keys store independent state."""
        mgr = self._make_manager()
        mgr.set_mpc("trv1__20", MpcState(gain_est=0.5))
        mgr.set_mpc("trv1__22", MpcState(gain_est=0.8))

        assert mgr.get_mpc("trv1__20").gain_est == 0.5
        assert mgr.get_mpc("trv1__22").gain_est == 0.8
