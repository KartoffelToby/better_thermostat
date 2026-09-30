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

import asyncio
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.const import (
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
    MIN_HEAT_LOSS,
    MIN_HEATING_POWER,
)
from custom_components.better_thermostat.utils.state_manager import (
    CURRENT_VERSION,
    MpcState,
    PIDState,
    RuntimeState,
    StateManager,
    ThermalStats,
    TpiState,
    _deserialize,
    _migrate_v0_to_v1,
    _serialize,
    deserialize_mpc,
    deserialize_mpc_v2,
    deserialize_pid,
    deserialize_tpi,
)

_SM = "custom_components.better_thermostat.utils.state_manager"


def _hass_double() -> AsyncMock:
    """Return a hass double whose event loop accepts timers.

    A copy that cannot be written starts a retry timer on ``hass.loop``;
    the ``AsyncMock`` default would turn ``loop.time()`` into a coroutine.
    """
    hass = AsyncMock()
    hass.loop = MagicMock()
    return hass


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
        mock_hass = _hass_double()
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
    async def test_load_survives_a_poisoned_store(self, caplog):
        """A store that breaks deserialization yields defaults, not a crash.

        load() runs inside the entity's startup task; an exception here
        would kill startup over data that relearning replaces anyway.
        The recovery is announced by a warning that carries the exception.
        """
        mgr, mock_store = self._make_manager_with_store()
        mock_store.async_load.return_value = {"version": 1, "mpc": {"k": {}}}
        with (
            caplog.at_level(logging.WARNING),
            _stores_by_key(),
            patch(
                "custom_components.better_thermostat.utils.state_manager._deserialize",
                side_effect=TypeError("poisoned"),
            ),
        ):
            await mgr.load()

        assert mgr.state.mpc == {}
        assert mgr.dirty is False
        assert "persisted state is unreadable, starting fresh" in caplog.text
        assert "poisoned" in caplog.text

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


# ---------------------------------------------------------------------------
# Controller bridging: clamped_thermal
# ---------------------------------------------------------------------------


def _make_manager() -> StateManager:
    """Create a StateManager with a mocked Store."""
    mock_hass = AsyncMock()
    with patch(f"{_SM}.Store"):
        return StateManager(mock_hass, "test_entry")


class TestClampedThermal:
    """clamped_thermal() returns persisted thermal stats clamped to valid bounds."""

    def test_both_none_returns_none(self):
        """Absent thermal stats yield (None, None)."""
        mgr = _make_manager()
        assert mgr.clamped_thermal() == (None, None)

    def test_valid_values_passed_through(self):
        """In-range values are returned unchanged."""
        mgr = _make_manager()
        hp = (MIN_HEATING_POWER + MAX_HEATING_POWER) / 2
        hl = (MIN_HEAT_LOSS + MAX_HEAT_LOSS) / 2
        mgr.thermal = ThermalStats(heating_power=hp, heat_loss_rate=hl)
        assert mgr.clamped_thermal() == (hp, hl)

    def test_heating_power_clamped_to_max(self):
        """A heating_power above the max is clamped down."""
        mgr = _make_manager()
        mgr.thermal = ThermalStats(heating_power=MAX_HEATING_POWER * 10)
        hp, _ = mgr.clamped_thermal()
        assert hp == MAX_HEATING_POWER

    def test_heating_power_clamped_to_min(self):
        """A heating_power below the min is clamped up."""
        mgr = _make_manager()
        mgr.thermal = ThermalStats(heating_power=-5.0)
        hp, _ = mgr.clamped_thermal()
        assert hp == MIN_HEATING_POWER

    def test_heat_loss_clamped_to_bounds(self):
        """heat_loss_rate is clamped to its min/max."""
        mgr = _make_manager()
        mgr.thermal = ThermalStats(heat_loss_rate=MAX_HEAT_LOSS * 10)
        assert mgr.clamped_thermal()[1] == MAX_HEAT_LOSS
        mgr.thermal = ThermalStats(heat_loss_rate=-1.0)
        assert mgr.clamped_thermal()[1] == MIN_HEAT_LOSS

    def test_unparseable_value_yields_none(self):
        """A non-numeric persisted value degrades to None instead of raising."""
        mgr = _make_manager()
        mgr.thermal = ThermalStats(heating_power="oops")  # type: ignore[arg-type]
        assert mgr.clamped_thermal()[0] is None

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_a_non_finite_value_yields_none(self, value):
        """A non-finite stat is no learned value; clamping must not pass it on.

        A NaN compares false against both bounds and would reach the entity
        unchanged, and an infinity would be taken for a learned extreme.
        """
        mgr = _make_manager()
        mgr.thermal = ThermalStats(heating_power=value, heat_loss_rate=value)
        assert mgr.clamped_thermal() == (None, None)


# ---------------------------------------------------------------------------
# Thermal stats recording
# ---------------------------------------------------------------------------


class TestRecordThermal:
    """record_thermal() stores the supplied stats; controller state stays put."""

    def test_records_thermal_and_dirties(self):
        """Supplied thermal stats are stored and the store is marked dirty."""
        mgr = _make_manager()
        mgr.record_thermal(0.07, 0.02)
        assert mgr.thermal.heating_power == 0.07
        assert mgr.thermal.heat_loss_rate == 0.02
        assert mgr.dirty is True

    def test_does_not_touch_controller_state(self):
        """MPC/PID/TPI state in the store stays untouched by record_thermal."""
        mgr = _make_manager()
        mgr.set_mpc("p:trv1", MpcState(gain_est=1.23))
        mgr.set_pid("p:trv1", PIDState(pid_kp=42.0))
        mgr.set_tpi("p:trv1", TpiState(last_percent=33.0))

        mgr.record_thermal(None, None)

        assert mgr.state.mpc["p:trv1"].gain_est == 1.23
        assert mgr.state.pid["p:trv1"].pid_kp == 42.0
        assert mgr.state.tpi["p:trv1"].last_percent == 33.0

    def test_non_finite_values_dropped_to_none(self):
        """NaN/inf samples are not persisted; finite ones are kept."""
        mgr = _make_manager()
        mgr.record_thermal(float("nan"), float("inf"))
        assert mgr.thermal.heating_power is None
        assert mgr.thermal.heat_loss_rate is None

        mgr.record_thermal(1500.0, 0.5)
        assert mgr.thermal.heating_power == 1500.0
        assert mgr.thermal.heat_loss_rate == 0.5


# ---------------------------------------------------------------------------
# PID state reset
# ---------------------------------------------------------------------------


class TestResetPidStates:
    """reset_pid_states() drops prefixed keys and reports the count."""

    def test_removes_only_prefixed_keys(self):
        """Keys with the prefix are removed; others stay."""
        mgr = _make_manager()
        mgr.set_pid("p:trv1:t21.0", PIDState())
        mgr.set_pid("p:trv1:t21.5", PIDState())
        mgr.set_pid("other:trvX:t20.0", PIDState())

        removed = mgr.reset_pid_states("p:")

        assert removed == 2
        assert set(mgr.state.pid) == {"other:trvX:t20.0"}

    def test_removal_marks_dirty(self):
        """Removing entries marks the store dirty."""
        mgr = _make_manager()
        mgr.set_pid("p:trv1:t21.0", PIDState())
        mgr._dirty = False

        mgr.reset_pid_states("p:")

        assert mgr.dirty is True

    def test_no_match_returns_zero_and_stays_clean(self):
        """Without matching keys nothing is removed and dirty stays False."""
        mgr = _make_manager()
        mgr.set_pid("other:trvX:t20.0", PIDState())
        mgr._dirty = False

        removed = mgr.reset_pid_states("p:")

        assert removed == 0
        assert mgr.dirty is False


class TestDeserializeRejectsNonFinite:
    """Persisted non-finite numbers never enter a restored state.

    A NaN that survives the restore poisons every value derived from it,
    so the deserializers keep the field default instead.
    """

    def test_mpc_nan_scalar_keeps_default(self):
        """A NaN in a stored MPC scalar is skipped."""
        mpc = deserialize_mpc({"gain_est": float("nan"), "last_percent": 40.0})
        assert mpc.gain_est is None
        assert mpc.last_percent == 40.0

    def test_pid_inf_integral_keeps_default(self):
        """An Inf in the stored PID integrator is skipped."""
        pid = deserialize_pid({"pid_integral": float("inf"), "pid_kp": 60.0})
        assert pid.pid_integral == 0.0
        assert pid.pid_kp == 60.0

    def test_tpi_nan_percent_keeps_default(self):
        """A NaN in the stored TPI duty cycle is skipped."""
        tpi = deserialize_tpi({"last_percent": float("nan")})
        assert tpi.last_percent is None

    def test_overflowing_int_keeps_default_and_spares_other_fields(self):
        """An int too large for float() is skipped, not propagated as OverflowError.

        Without catching OverflowError the bad field escapes the per-field
        guard and load() resets the whole store; only the offending field
        must be dropped.
        """
        mpc = deserialize_mpc({"gain_est": 10**400, "last_percent": 40.0})
        assert mpc.gain_est is None
        assert mpc.last_percent == 40.0


def _warnings(caplog) -> list[str]:
    """Return the WARNING-or-worse messages the state manager logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name == _SM
    ]


class TestDroppedStoredValuesAreReported:
    """A stored value the load cannot use is named when it is dropped.

    Past the load path a dropped field or entry carries the default a first
    start leaves there, so a value the store lost looks exactly like one it
    never held. The load is the only place that can still say so.
    """

    @pytest.mark.parametrize(
        ("deserialize", "raw", "field"),
        [
            pytest.param(deserialize_mpc, {"gain_est": "abc"}, "gain_est", id="mpc"),
            pytest.param(
                deserialize_mpc,
                {"gain_est": float("nan")},
                "gain_est",
                id="mpc-non-finite",
            ),
            pytest.param(deserialize_pid, {"pid_kp": "abc"}, "pid_kp", id="pid"),
            pytest.param(
                deserialize_pid,
                {"pid_integral": float("inf")},
                "pid_integral",
                id="pid-non-finite",
            ),
            pytest.param(
                deserialize_tpi, {"last_percent": "bad"}, "last_percent", id="tpi"
            ),
            pytest.param(
                deserialize_tpi,
                {"last_percent": float("nan")},
                "last_percent",
                id="tpi-non-finite",
            ),
            pytest.param(
                deserialize_mpc_v2, {"created_ts": "later"}, "created_ts", id="mpc_v2"
            ),
            pytest.param(
                deserialize_mpc_v2,
                {"snapshot": "garbage"},
                "snapshot",
                id="mpc_v2-snapshot",
            ),
        ],
    )
    def test_an_unreadable_field_is_named_with_its_key(
        self, caplog, deserialize, raw, field
    ):
        """A field the load cannot use keeps its default and is reported.

        The report names the field and the entry it belonged to.
        """
        with caplog.at_level(logging.DEBUG, logger=_SM):
            deserialize(raw, key="room_key")

        assert any(
            field in message and "room_key" in message for message in _warnings(caplog)
        ), _warnings(caplog)

    @pytest.mark.parametrize("section", ["mpc", "mpc_v2", "pid", "tpi"])
    def test_a_misshapen_entry_is_named(self, caplog, section):
        """An entry that is not a mapping is dropped and reported with its key."""
        raw = {"version": 1, section: {"room_key": "not_a_dict"}}
        with caplog.at_level(logging.DEBUG, logger=_SM):
            state = _deserialize(raw)

        assert getattr(state, section) == {}
        assert any(
            section in message and "room_key" in message
            for message in _warnings(caplog)
        ), _warnings(caplog)

    @pytest.mark.parametrize(
        "section", ["mpc", "mpc_v2", "pid", "tpi", "thermal", "presets"]
    )
    def test_a_misshapen_section_is_named(self, caplog, section):
        """A whole section of the wrong shape is dropped and reported by name."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            _deserialize({"version": 1, section: "garbage"})

        assert any(section in message for message in _warnings(caplog)), _warnings(
            caplog
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("heating_power", "later"),
            ("heat_loss_rate", "Infinity"),
            ("heat_loss_rate", 1e400),
            ("heating_power", [20.0]),
        ],
    )
    def test_an_unusable_thermal_value_is_named(self, caplog, field, value):
        """A stored thermal value that is not a finite number is named."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            state = _deserialize({"version": 1, "thermal": {field: value}})

        assert getattr(state.thermal, field) is None
        assert any(
            "thermal" in message and field in message for message in _warnings(caplog)
        ), _warnings(caplog)

    def test_an_unusable_preset_is_named(self, caplog):
        """A stored preset temperature that is not a finite number is named."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            state = _deserialize(
                {"version": 1, "presets": {"eco": "warm", "comfort": 21.0}}
            )

        assert state.presets == {"comfort": 21.0}
        assert any(
            "presets" in message and "eco" in message for message in _warnings(caplog)
        ), _warnings(caplog)

    def test_a_null_thermal_value_is_not_reported(self, caplog):
        """A null thermal value is a value not yet learned, not a lost one."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            _deserialize(
                {
                    "version": 1,
                    "thermal": {"heating_power": None, "heat_loss_rate": None},
                }
            )

        assert _warnings(caplog) == []

    @pytest.mark.parametrize(
        ("section", "entry", "field"),
        [
            ("mpc", {"gain_est": float("nan")}, "gain_est"),
            ("mpc_v2", {"created_ts": float("inf")}, "created_ts"),
            ("pid", {"pid_kp": float("nan")}, "pid_kp"),
            ("tpi", {"last_update_ts": float("nan")}, "last_update_ts"),
        ],
    )
    def test_a_load_names_the_entry_of_a_dropped_value(
        self, caplog, section, entry, field
    ):
        """A value the store load drops is reported with its section and key."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            _deserialize({"version": 1, section: {"room_key": entry}})

        assert any(
            section in message and "room_key" in message and field in message
            for message in _warnings(caplog)
        ), _warnings(caplog)


# ---------------------------------------------------------------------------
# Unreadable store handling
# ---------------------------------------------------------------------------

_LIVE_STORE_KEY = "better_thermostat_test_entry_state"
_SET_ASIDE_KEY = "better_thermostat_test_entry_state.corrupt"
_SET_ASIDE_KEYS = (_SET_ASIDE_KEY, f"{_SET_ASIDE_KEY}.1", f"{_SET_ASIDE_KEY}.2")
"""The keys the copies of one entry's unreadable payloads are kept under."""


def _saved_into(store: AsyncMock):
    """Return a save side effect after which *store* loads what was saved."""

    def _save(data):
        store.async_load.return_value = data

    return _save


def _truncated_into(store: AsyncMock):
    """Return a save side effect after which *store* loads a cut-off payload."""

    def _save(_data):
        store.async_load.return_value = {"version": 1}

    return _save


@contextmanager
def _stores_by_key():
    """Patch Store so every construction is recorded under its storage key.

    A store nobody has written yet loads as ``None``, which is what Home
    Assistant returns for a missing storage file.
    """
    stores: dict[str, AsyncMock] = {}

    def _store_for(_hass, _version, key, **_options):
        if key not in stores:
            store = AsyncMock()
            store.async_load = AsyncMock(return_value=None)
            store.async_save = AsyncMock(side_effect=_saved_into(store))
            stores[key] = store
        return stores[key]

    with patch(f"{_SM}.Store", side_effect=_store_for):
        yield stores


def _poisoned_payload(gain: float) -> dict:
    """Return a stored payload that loads with one value dropped."""
    return {"version": 1, "mpc": {"k1": {"gain_est": gain, "kalman_P": "NaN"}}}


class TestUnreadableStoreIsKeptForRecovery:
    """An unreadable store is copied aside before defaults take its place.

    Falling back to defaults is deliberate -- startup must not die over a
    damaged file -- but the next save writes those defaults over the live
    store, so without a copy the only record of what an installation had
    learned is gone.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["1", None, [1]])
    async def test_an_unreadable_store_is_copied_verbatim_and_named(
        self, caplog, version
    ):
        """The copy holds the payload as stored, and the log names the copy."""
        payload = {"version": version, "mpc": {"k1": {"gain_est": 0.5}}}
        with _stores_by_key() as stores, caplog.at_level(logging.WARNING, logger=_SM):
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            await mgr.load()

        assert mgr.state.mpc == {}
        assert mgr.dirty is False
        stores[_SET_ASIDE_KEY].async_save.assert_awaited_once()
        assert stores[_SET_ASIDE_KEY].async_save.await_args[0][0] == payload
        assert any(_SET_ASIDE_KEY in message for message in _warnings(caplog))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            _poisoned_payload(0.5),
            {"version": 1, "pid": {"k1": {"pid_kp": float("inf")}}},
            {"version": 1, "mpc": {"k1": "not_a_dict"}},
            {"version": 1, "pid": ["not", "a", "mapping"]},
            {"version": 1, "thermal": "not_a_mapping"},
            {"version": 1, "thermal": {"heating_power": "NaN"}},
            {"version": 1, "presets": {"eco": "warm"}},
            {"version": 1, "mpc_v2": {"k1": {"snapshot": "garbage"}}},
        ],
    )
    async def test_a_dropped_part_is_set_aside_before_it_is_lost(self, payload):
        """A value, entry or section the load drops is kept aside as well.

        What was dropped starts from defaults, and those overwrite the
        stored payload on the next save.
        """
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            await mgr.load()

        assert _SET_ASIDE_KEY in stores
        stores[_SET_ASIDE_KEY].async_save.assert_awaited_once()
        assert stores[_SET_ASIDE_KEY].async_save.await_args[0][0] == payload

    @pytest.mark.asyncio
    async def test_a_readable_store_leaves_no_copy(self):
        """Nothing is set aside when every stored value loads."""
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": 1,
                "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}},
                "thermal": {"heating_power": None},
                "presets": {"eco": 17.0},
            }
            await mgr.load()
            mgr.mark_dirty()
            await mgr.save()

        assert not set(_SET_ASIDE_KEYS) & set(stores)
        assert mgr.state.mpc["k1"].gain_est == 0.5
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_empty_store_leaves_no_copy(self):
        """A first start has nothing to preserve."""
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            await mgr.load()

        assert not set(_SET_ASIDE_KEYS) & set(stores)

    @staticmethod
    def _failing_copy(stores, *failures: Exception) -> AsyncMock:
        """Install a set-aside store whose writes raise *failures* in turn."""
        copy = AsyncMock()
        copy.async_load = AsyncMock(return_value=None)
        pending = list(failures)
        save = _saved_into(copy)

        def _write(data):
            if pending:
                raise pending.pop(0)
            save(data)

        copy.async_save = AsyncMock(side_effect=_write)
        stores[_SET_ASIDE_KEY] = copy
        return copy

    @pytest.mark.asyncio
    async def test_a_failed_copy_still_lets_startup_continue(self, caplog):
        """A storage error while copying is reported, not raised."""
        with _stores_by_key() as stores, caplog.at_level(logging.WARNING, logger=_SM):
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {"version": "1"}
            self._failing_copy(stores, HomeAssistantError("disk full"))
            await mgr.load()

        assert mgr.state.mpc == {}
        assert "could not set the unreadable state aside" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("poisoned_by", ["a dropped value", "an unreadable store"])
    async def test_the_live_store_is_kept_while_no_copy_exists(self, poisoned_by):
        """A save does not overwrite a payload that could not be set aside.

        The copy is taken again before the save; while it keeps failing,
        the stored payload stays the only record of what was learned, and
        the state waits unsaved.
        """
        payload = _poisoned_payload(0.5)
        if poisoned_by == "an unreadable store":
            payload["version"] = "1"
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            self._failing_copy(stores, OSError("disk full"), OSError("disk full"))
            await mgr.load()
            mgr.mark_dirty()

            await mgr.save()

        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()
        assert mgr.dirty is True

    @pytest.mark.asyncio
    async def test_a_copy_that_succeeds_later_lets_the_save_through(self):
        """Once the payload is set aside, the live store is written again."""
        payload = _poisoned_payload(0.5)
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            copy = self._failing_copy(stores, OSError("disk full"))
            await mgr.load()
            mgr.mark_dirty()

            await mgr.save()

        assert copy.async_save.await_count == 2
        assert copy.async_save.await_args[0][0] == payload
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()
        assert mgr.dirty is False


def _records(caplog, level: int) -> list[logging.LogRecord]:
    """Return the state manager's log records of exactly *level*."""
    return [r for r in caplog.records if r.name == _SM and r.levelno == level]


def _copy_warnings(caplog) -> list[logging.LogRecord]:
    """Return the WARNING records about a copy that could not be set aside."""
    return [
        r
        for r in _records(caplog, logging.WARNING)
        if "could not set the unreadable state aside" in r.getMessage()
        or "did not reach" in r.getMessage()
    ]


class TestAFailingCopyIsReportedOnce:
    """While the copy keeps failing, the log says so once, not on every save.

    Runtime saves wait at DEBUG for a copy that cannot be written; the copy
    is tried again at a growing interval and when the entity flushes on stop
    or removal.
    """

    _PAYLOAD = _poisoned_payload(0.5)

    @staticmethod
    def _copy_failing(stores, failure: Exception | None) -> AsyncMock:
        """Install a set-aside store that raises *failure* or cuts the write."""
        copy = AsyncMock()
        copy.async_load = AsyncMock(return_value=None)
        if failure is not None:
            copy.async_save = AsyncMock(side_effect=failure)
        else:
            copy.async_save = AsyncMock(side_effect=_truncated_into(copy))
        for key in _SET_ASIDE_KEYS:
            stores[key] = copy
        return copy

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [OSError("disk full"), HomeAssistantError("permission denied"), None],
        ids=["os-error", "store-error", "read-back-differs"],
    )
    async def test_twenty_runtime_saves_log_one_warning(self, caplog, failure):
        """One WARNING without traceback; the saves are skipped at DEBUG."""
        with _stores_by_key() as stores, caplog.at_level(logging.DEBUG, logger=_SM):
            copy = self._copy_failing(stores, failure)
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
            await mgr.load()
            for _ in range(20):
                mgr.mark_dirty()
                await mgr.save_if_dirty()

        warnings = _copy_warnings(caplog)
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        assert warnings[0].exc_info is None
        skipped = [
            r
            for r in _records(caplog, logging.DEBUG)
            if "save skipped" in r.getMessage()
        ]
        assert len(skipped) == 20
        assert copy.async_save.await_count == 1
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()
        assert mgr.dirty is True

    @pytest.mark.asyncio
    async def test_the_traceback_of_the_failure_is_kept_at_debug(self, caplog):
        """The storage error behind the warning is still in the debug log."""
        with _stores_by_key() as stores, caplog.at_level(logging.DEBUG, logger=_SM):
            self._copy_failing(stores, OSError("disk full"))
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
            await mgr.load()

        traced = [
            r
            for r in _records(caplog, logging.DEBUG)
            if r.exc_info and "unreadable state aside" in r.getMessage()
        ]
        assert len(traced) == 1
        assert "disk full" in str(traced[0].exc_info[1])

    @pytest.mark.asyncio
    async def test_a_flush_that_fails_again_adds_no_warning(self, caplog):
        """The flush retries the copy; a second failure is logged at DEBUG."""
        with _stores_by_key() as stores, caplog.at_level(logging.DEBUG, logger=_SM):
            copy = self._copy_failing(stores, OSError("disk full"))
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
            await mgr.load()
            mgr.mark_dirty()
            await mgr.flush()

        assert copy.async_save.await_count == 2
        assert len(_copy_warnings(caplog)) == 1
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_flush_retries_the_copy_and_then_saves(self):
        """Once the copy succeeds on the flush, the state is written."""
        with _stores_by_key() as stores:
            copy = self._copy_failing(stores, OSError("disk full"))
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
            await mgr.load()
            mgr.mark_dirty()
            await mgr.save_if_dirty()
            copy.async_save.side_effect = _saved_into(copy)
            await mgr.flush()

        assert copy.async_load.return_value == self._PAYLOAD
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()
        assert mgr.dirty is False

    async def test_a_full_disk_on_home_assistant_storage_logs_once(
        self, hass, hass_storage, caplog
    ):
        """On a real store, twenty saves give one warning and no store errors."""
        live = "better_thermostat_copy_entry_state"
        hass_storage[live] = {
            "version": 1,
            "minor_version": 1,
            "key": live,
            "data": self._PAYLOAD,
        }
        write = storage.Store._async_write_data

        async def _write(store, data):
            if ".corrupt" in store.key:
                raise WriteError("disk full")
            await write(store, data)

        with (
            patch.object(storage.Store, "_async_write_data", _write),
            caplog.at_level(logging.DEBUG),
        ):
            mgr = StateManager(hass, "copy_entry")
            await mgr.load()
            for _ in range(20):
                mgr.mark_dirty()
                await mgr.save_if_dirty()

        assert len(_copy_warnings(caplog)) == 1
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, [r.getMessage() for r in errors]
        assert hass_storage[live]["data"] == self._PAYLOAD


class _Clock:
    """A settable stand-in for the monotonic clock the retries are timed on."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestAFailedCopyThatRecovers:
    """Once the copy can be written again, the session's state is saved.

    A copy that failed at load is retried on a timer, and by a runtime save
    that falls due first, after a minute and then at a doubling interval, so
    a disk that stays full is not written to on every save.
    """

    _PAYLOAD = _poisoned_payload(0.5)

    @staticmethod
    def _copy_store(stores, disk: dict) -> AsyncMock:
        copy = AsyncMock()
        copy.async_load = AsyncMock(return_value=None)
        save = _saved_into(copy)

        def _write(data):
            if disk["full"]:
                raise OSError("disk full")
            save(data)

        copy.async_save = AsyncMock(side_effect=_write)
        for key in _SET_ASIDE_KEYS:
            stores[key] = copy
        return copy

    async def _loaded(self, stores, disk):
        copy = self._copy_store(stores, disk)
        mgr = StateManager(_hass_double(), "test_entry")
        stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
        await mgr.load()
        return mgr, copy

    @pytest.mark.asyncio
    async def test_a_runtime_save_after_recovery_lands(self):
        """The first runtime save once the retry is due writes copy and state."""
        clock = _Clock()
        disk = {"full": True}
        with _stores_by_key() as stores, patch(f"{_SM}.monotonic", clock):
            mgr, copy = await self._loaded(stores, disk)
            disk["full"] = False
            mgr.mark_dirty()
            await mgr.save_if_dirty()
            assert copy.async_save.await_count == 1
            stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()

            clock.now += 60
            await mgr.save_if_dirty()

        assert copy.async_load.return_value == self._PAYLOAD
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_the_retry_interval_doubles_while_the_copy_fails(self):
        """A disk that stays full is tried ever less often."""
        clock = _Clock()
        disk = {"full": True}
        attempts: list[float] = []
        with _stores_by_key() as stores, patch(f"{_SM}.monotonic", clock):
            mgr, copy = await self._loaded(stores, disk)
            start = clock.now
            for _ in range(500):
                clock.now += 1
                done = copy.async_save.await_count
                mgr.mark_dirty()
                await mgr.save_if_dirty()
                if copy.async_save.await_count > done:
                    attempts.append(clock.now - start)

        assert attempts == [60, 180, 420]
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_interval_stops_growing_at_an_hour(self):
        """A disk that stays full is retried once an hour at most, not less."""
        clock = _Clock()
        disk = {"full": True}
        with _stores_by_key() as stores, patch(f"{_SM}.monotonic", clock):
            mgr, copy = await self._loaded(stores, disk)
            for _ in range(20):
                clock.now += 3600
                mgr.mark_dirty()
                await mgr.save_if_dirty()

        assert copy.async_save.await_count == 21

    @pytest.mark.asyncio
    async def test_no_copy_is_attempted_while_home_assistant_stops(self):
        """While stopping, a copy would only be queued, so it is left for later."""
        disk = {"full": False}
        with _stores_by_key() as stores:
            copy = self._copy_store(stores, disk)
            hass = _hass_double()
            hass.state = CoreState.running
            mgr = StateManager(hass, "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
            copy.async_save.side_effect = OSError("disk full")
            await mgr.load()
            copy.async_save.side_effect = _saved_into(copy)
            hass.state = CoreState.stopping
            mgr.mark_dirty()
            await mgr.flush()

        assert copy.async_save.await_count == 1
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()
        assert mgr.dirty is True

    async def _loaded_on(self, hass, stores, disk):
        """Load the unreadable payload on a real hass, whose timers run."""
        copy = self._copy_store(stores, disk)
        mgr = StateManager(hass, "test_entry")
        stores[_LIVE_STORE_KEY].async_load.return_value = self._PAYLOAD
        await mgr.load()
        return mgr, copy

    @staticmethod
    async def _advance(hass, freezer, seconds: float) -> None:
        """Move the clock on by *seconds* and let what fell due run."""
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done(wait_background_tasks=True)

    @pytest.mark.asyncio
    async def test_the_copy_and_the_unsaved_state_land_on_their_own_timer(
        self, hass, freezer
    ):
        """Learning skipped for the copy is saved once the retry is due.

        No further change of state is needed: until something else triggers
        a save, an abrupt stop would otherwise lose that learning.
        """
        disk = {"full": True}
        with _stores_by_key() as stores:
            mgr, copy = await self._loaded_on(hass, stores, disk)
            mgr.mark_dirty()
            await mgr.save_if_dirty()
            disk["full"] = False

            await self._advance(hass, freezer, 30)
            assert copy.async_save.await_count == 1
            stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()

            await self._advance(hass, freezer, 31)

        assert copy.async_load.return_value == self._PAYLOAD
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_the_timer_backs_off_while_the_copy_fails(self, hass, freezer):
        """Each failed timed try waits twice as long as the one before."""
        disk = {"full": True}
        attempts: list[int] = []
        with _stores_by_key() as stores:
            mgr, copy = await self._loaded_on(hass, stores, disk)
            mgr.mark_dirty()
            for second in range(1, 450):
                done = copy.async_save.await_count
                await self._advance(hass, freezer, 1)
                if copy.async_save.await_count > done:
                    attempts.append(second)
            mgr.close()

        assert attempts == [60, 180, 420]
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_closed_manager_leaves_no_timer_behind(self, hass, freezer):
        """A removed entity's manager tries the copy when flushed, not on a timer.

        A timer that outlived the entity would write into a store the next
        entity for the same entry owns by then.
        """
        disk = {"full": True}
        with _stores_by_key() as stores:
            mgr, copy = await self._loaded_on(hass, stores, disk)
            mgr.mark_dirty()
            mgr.close()
            await self._advance(hass, freezer, 2 * 3600)
            tried_before_the_flush = copy.async_save.await_count

            await mgr.flush()
            await self._advance(hass, freezer, 2 * 3600)

        assert tried_before_the_flush == 1
        assert copy.async_save.await_count == 2
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_copy_that_lands_after_close_saves_nothing(self, hass, freezer):
        """A timed copy still under way when the entity goes saves nothing.

        ``flush()`` makes the final write; a save made by the copy after
        ``close()`` would write into a store the next entity owns by then.
        """
        disk = {"full": True}
        entered = asyncio.Event()
        release = asyncio.Event()
        with _stores_by_key() as stores:
            mgr, copy = await self._loaded_on(hass, stores, disk)
            mgr.mark_dirty()
            kept = _saved_into(copy)

            async def _gated(data):
                entered.set()
                await release.wait()
                kept(data)

            copy.async_save.side_effect = _gated
            freezer.tick(timedelta(seconds=61))
            async_fire_time_changed(hass, dt_util.utcnow())
            await asyncio.wait_for(entered.wait(), timeout=5)

            mgr.close()
            release.set()
            await hass.async_block_till_done(wait_background_tasks=True)
            await self._advance(hass, freezer, 2 * 3600)

        assert copy.async_load.return_value == self._PAYLOAD
        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()
        assert mgr.dirty is True

    @pytest.mark.asyncio
    async def test_the_final_flush_waits_for_a_copy_under_way(self, hass, freezer):
        """A flush during a timed copy saves the state once that copy lands.

        A second try beside the running one may fail where the first one
        succeeds; the running copy then saves nothing after ``close()``, so
        the flush has to wait for it and write the state itself.
        """
        disk = {"full": True}
        entered = asyncio.Event()
        release = asyncio.Event()
        with _stores_by_key() as stores:
            mgr, copy = await self._loaded_on(hass, stores, disk)
            mgr.mark_dirty()
            kept = _saved_into(copy)
            gated_writes = {"count": 0}

            async def _gated(data):
                gated_writes["count"] += 1
                if gated_writes["count"] > 1:
                    raise OSError("disk busy")
                entered.set()
                await release.wait()
                kept(data)

            copy.async_save.side_effect = _gated
            freezer.tick(timedelta(seconds=61))
            async_fire_time_changed(hass, dt_util.utcnow())
            await asyncio.wait_for(entered.wait(), timeout=5)

            mgr.close()
            flush = hass.async_create_task(mgr.flush())
            for _ in range(20):
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(flush, timeout=5)
            saved_by_the_flush = stores[_LIVE_STORE_KEY].async_save.await_count

            await hass.async_block_till_done(wait_background_tasks=True)
            await self._advance(hass, freezer, 2 * 3600)

        assert copy.async_load.return_value == self._PAYLOAD
        assert saved_by_the_flush == 1
        assert stores[_LIVE_STORE_KEY].async_save.await_count == 1
        assert mgr.dirty is False


class TestEveryDistinctPayloadIsKept:
    """Each distinct unreadable payload gets a copy before the store is written.

    A payload read after an earlier copy was taken holds what was learned
    since, and the next save replaces it as much as it replaced the first.
    """

    @staticmethod
    async def _load_and_save(stores, payload: dict) -> StateManager:
        mgr = StateManager(_hass_double(), "test_entry")
        stores[_LIVE_STORE_KEY].async_load.return_value = payload
        await mgr.load()
        mgr.mark_dirty()
        await mgr.save()
        return mgr

    @staticmethod
    def _stored(stores, key: str, payload: dict) -> None:
        """Seed an existing copy under *key*."""
        store = AsyncMock()
        store.async_load = AsyncMock(return_value=payload)
        store.async_save = AsyncMock(side_effect=_saved_into(store))
        stores[key] = store

    @pytest.mark.asyncio
    async def test_a_newer_payload_is_kept_beside_the_first_copy(self):
        """The first copy stays, and the newer payload gets one of its own."""
        with _stores_by_key() as stores:
            self._stored(stores, _SET_ASIDE_KEYS[0], _poisoned_payload(0.1))
            await self._load_and_save(stores, _poisoned_payload(0.5))

        stores[_SET_ASIDE_KEYS[0]].async_save.assert_not_awaited()
        assert stores[_SET_ASIDE_KEYS[1]].async_load.return_value == (
            _poisoned_payload(0.5)
        )
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_payload_already_kept_gets_no_second_copy(self):
        """Loading the same unreadable payload again adds no copy."""
        with _stores_by_key() as stores:
            self._stored(stores, _SET_ASIDE_KEYS[0], _poisoned_payload(0.1))
            self._stored(stores, _SET_ASIDE_KEYS[1], _poisoned_payload(0.5))
            await self._load_and_save(stores, _poisoned_payload(0.5))

        for key in _SET_ASIDE_KEYS:
            if key in stores:
                stores[key].async_save.assert_not_awaited()
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_with_every_copy_taken_the_newest_is_replaced(self):
        """The first copies stay; the current payload takes the newest one's place.

        The first copy holds the state from before anything went wrong, and
        the current payload the most recent learning.
        """
        with _stores_by_key() as stores:
            for gain, key in zip((0.1, 0.2, 0.3), _SET_ASIDE_KEYS, strict=True):
                self._stored(stores, key, _poisoned_payload(gain))
            await self._load_and_save(stores, _poisoned_payload(0.5))

        stores[_SET_ASIDE_KEYS[0]].async_save.assert_not_awaited()
        stores[_SET_ASIDE_KEYS[1]].async_save.assert_not_awaited()
        assert stores[_SET_ASIDE_KEYS[2]].async_load.return_value == (
            _poisoned_payload(0.5)
        )
        stores[_LIVE_STORE_KEY].async_save.assert_awaited_once()
        assert f"{_SET_ASIDE_KEY}.3" not in stores

    @pytest.mark.asyncio
    async def test_a_copy_that_reads_back_different_keeps_the_live_store(self):
        """Only a copy that reads back as the payload lets the save through."""
        with _stores_by_key() as stores:
            for key in _SET_ASIDE_KEYS:
                copy = AsyncMock()
                copy.async_load = AsyncMock(return_value=None)
                copy.async_save = AsyncMock(side_effect=_truncated_into(copy))
                stores[key] = copy
            mgr = await self._load_and_save(stores, _poisoned_payload(0.5))

        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()
        assert mgr.dirty is True


class TestTheCopyIsConfirmedOnTheHomeAssistantStore:
    """The live store is overwritten only once the copy is on disk.

    Home Assistant's ``Store.async_save`` returns normally when the write
    fails, and defers the write to the final-write event while Home
    Assistant is stopping, so a returned save confirms no copy.
    """

    _LIVE_KEY = "better_thermostat_copy_entry_state"
    _COPY_KEY = "better_thermostat_copy_entry_state.corrupt"
    _PAYLOAD = _poisoned_payload(0.5)

    @contextmanager
    def _copy_writes_fail(self):
        """Make every write of the set-aside copy fail as a full disk does."""
        write = storage.Store._async_write_data

        async def _write(store, data):
            if store.key == self._COPY_KEY:
                raise WriteError("disk full")
            await write(store, data)

        with patch.object(storage.Store, "_async_write_data", _write):
            yield

    async def _loaded_manager(self, hass, hass_storage) -> StateManager:
        hass_storage[self._LIVE_KEY] = {
            "version": 1,
            "minor_version": 1,
            "key": self._LIVE_KEY,
            "data": self._PAYLOAD,
        }
        manager = StateManager(hass, "copy_entry")
        await manager.load()
        manager.mark_dirty()
        return manager

    async def test_a_failed_copy_write_keeps_the_live_store(self, hass, hass_storage):
        """A copy the store could not write lets no save through."""
        with self._copy_writes_fail():
            manager = await self._loaded_manager(hass, hass_storage)
            await manager.save()

        assert self._COPY_KEY not in hass_storage
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD
        assert manager.dirty is True

    async def test_a_save_while_stopping_keeps_the_live_store(self, hass, hass_storage):
        """A copy deferred to the final write is no copy yet.

        Both writes would then land on the final-write event, and a copy
        that fails there would leave the live store overwritten.
        """
        with self._copy_writes_fail():
            manager = await self._loaded_manager(hass, hass_storage)
            hass.set_state(CoreState.stopping)
            try:
                await manager.save()
                hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
                await hass.async_block_till_done()
            finally:
                hass.set_state(CoreState.running)

        assert self._COPY_KEY not in hass_storage
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD

    async def test_a_written_copy_lets_the_save_through(self, hass, hass_storage):
        """Once the copy is on disk, the save replaces the live store."""
        manager = await self._loaded_manager(hass, hass_storage)
        await manager.save()

        assert hass_storage[self._COPY_KEY]["data"] == self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] != self._PAYLOAD
        assert manager.dirty is False

    async def test_an_older_copy_and_a_newer_payload_are_both_kept(
        self, hass, hass_storage
    ):
        """A newer unreadable payload is set aside beside an older copy."""
        older = _poisoned_payload(0.1)
        hass_storage[self._COPY_KEY] = {
            "version": 1,
            "minor_version": 1,
            "key": self._COPY_KEY,
            "data": older,
        }
        manager = await self._loaded_manager(hass, hass_storage)
        await manager.save()

        assert hass_storage[self._COPY_KEY]["data"] == older
        assert hass_storage[f"{self._COPY_KEY}.1"]["data"] == self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] != self._PAYLOAD
