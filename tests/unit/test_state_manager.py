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
from contextlib import contextmanager
from dataclasses import asdict
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import json_bytes, prepare_save_json
from homeassistant.util.json import json_loads
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import MpcV2Params
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    ControllerSnapshot,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    GAIN_HEATER_BOUNDS,
    TAU_ROOM_BOUNDS_MIN,
)
from custom_components.better_thermostat.utils.const import (
    MAX_HEAT_LOSS,
    MAX_HEATING_POWER,
    MIN_HEAT_LOSS,
    MIN_HEATING_POWER,
)
from custom_components.better_thermostat.utils.state_manager import (
    _MAX_STORED_INT,
    _MIN_STORED_INT,
    _MPC_NULLABLE_FIELDS,
    _MPC_V2_NULLABLE_FIELDS,
    _MPC_V2_REID_NULLABLE_FIELDS,
    _PID_NULLABLE_FIELDS,
    _TPI_NULLABLE_FIELDS,
    CURRENT_VERSION,
    MpcState,
    MpcV2StateData,
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
    deserialize_mpc_v2_reid,
    deserialize_pid,
    deserialize_tpi,
)

_SM = "custom_components.better_thermostat.utils.state_manager"

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

    def test_legacy_presets_section_ignored(self):
        """A legacy presets section in a stored payload is ignored.

        Preset temperatures live in the preset number entities.
        """
        raw = _serialize(RuntimeState())
        raw["presets"] = {"comfort": 22.0}

        restored = _deserialize(raw)

        assert not hasattr(restored, "presets")

    def test_thermal_rejects_non_finite(self):
        """NaN/inf thermal stats in a stored payload are rejected on load."""
        raw = _serialize(RuntimeState())
        raw["thermal"] = {"heating_power": float("nan"), "heat_loss_rate": float("inf")}

        restored = _deserialize(raw)

        assert restored.thermal.heating_power is None
        assert restored.thermal.heat_loss_rate is None

    def test_full_state_roundtrip(self):
        """Complete state with all sections populated."""
        original = RuntimeState(
            mpc={"k1": MpcState(gain_est=0.5, loss_est=0.02)},
            pid={"k1": PIDState(pid_kp=2.0)},
            tpi={"k1": TpiState(last_percent=30.0)},
            thermal=ThermalStats(heating_power=800.0),
        )

        raw = _serialize(original)
        restored = _deserialize(raw)

        assert restored.mpc["k1"].gain_est == 0.5
        assert restored.pid["k1"].pid_kp == 2.0
        assert restored.tpi["k1"].last_percent == 30.0
        assert restored.thermal.heating_power == 800.0


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


class TestDeserializeMpcV2Reid:
    """deserialize_mpc_v2_reid should discard entries with corrupt math."""

    def test_happy_path(self):
        """A plausible payload is restored field by field."""
        raw = {
            "tau_room_min": 240.0,
            "gain_heater": 3.0,
            "fitted_ts": 1000.0,
            "rmse_prior_K": 0.4,
            "rmse_fit_K": 0.1,
            "n_segments": 4,
        }
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.tau_room_min == 240.0
        assert reid.gain_heater == 3.0
        assert reid.fitted_ts == 1000.0
        assert reid.rmse_prior_K == 0.4
        assert reid.rmse_fit_K == 0.1
        assert reid.n_segments == 4

    def test_nan_tau_room_discards_the_entry(self):
        """NaN passes every ``<=`` comparison, so the gate cannot catch it."""
        raw = {"tau_room_min": float("nan"), "gain_heater": 3.0}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_infinite_gain_discards_the_entry(self):
        """An infinite heater gain would blow up the plant prior."""
        raw = {"tau_room_min": 240.0, "gain_heater": float("inf")}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_non_finite_secondary_field_discards_the_entry(self):
        """A corrupt validation metric taints the fit it belongs to."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "rmse_fit_K": float("-inf")}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_wrong_type_only_skips_the_field(self):
        """A wrong type is schema drift, not corrupt math: keep the entry."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "rmse_fit_K": "later"}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.tau_room_min == 240.0
        assert reid.rmse_fit_K == 0.0

    def test_wrong_type_only_skips_the_segment_count(self):
        """The count is metadata, so an unreadable one still keeps the entry."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": "four"}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.n_segments == 0

    def test_null_fitted_timestamp_discards_the_entry(self):
        """``fitted_ts`` is typed ``float``, so its null is a saved NaN."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "fitted_ts": None}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_null_validation_metric_discards_the_entry(self):
        """A null in either RMSE taints the fit those metrics accepted."""
        base = {"tau_room_min": 240.0, "gain_heater": 3.0}
        assert deserialize_mpc_v2_reid({**base, "rmse_prior_K": None}) is None
        assert deserialize_mpc_v2_reid({**base, "rmse_fit_K": None}) is None

    def test_null_segment_count_discards_the_entry(self):
        """``n_segments`` is typed ``int``; a null is not a tally either."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": None}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_null_prior_component_is_refused_as_a_null(self, caplog):
        """A null is refused on its own terms, not by the positivity gate."""
        with caplog.at_level(logging.WARNING, logger=_SM):
            assert deserialize_mpc_v2_reid({"tau_room_min": None}) is None
        assert "tau_room_min" in caplog.text
        assert "is null" in caplog.text

    def test_absent_field_is_not_a_null(self):
        """A field the payload never carried keeps its default, entry intact."""
        reid = deserialize_mpc_v2_reid({"tau_room_min": 240.0, "gain_heater": 3.0})
        assert reid is not None
        assert reid.fitted_ts == 0.0
        assert reid.rmse_prior_K == 0.0
        assert reid.rmse_fit_K == 0.0
        assert reid.n_segments == 0

    def test_null_entry_is_absent_after_a_full_load(self):
        """A saved NaN comes back as a null and leaves no key behind."""
        raw = _serialize(RuntimeState())
        raw["mpc_v2_reid"] = {
            "good": {"tau_room_min": 240.0, "gain_heater": 3.0},
            "bad": {"tau_room_min": 240.0, "gain_heater": 3.0, "rmse_fit_K": None},
        }
        restored = _deserialize(raw)
        assert "bad" not in restored.mpc_v2_reid
        assert restored.mpc_v2_reid["good"].tau_room_min == 240.0

    def test_tiny_positive_time_constant_is_rejected(self):
        """A positive time constant this small still divides the room dynamics."""
        raw = {"tau_room_min": 5e-324, "gain_heater": 3.0}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_time_constant_above_the_band_is_rejected(self):
        """Too slow an envelope freezes the dynamics as surely as too fast."""
        raw = {"tau_room_min": 1e300, "gain_heater": 3.0}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_heater_gain_below_the_band_is_rejected(self):
        """A gain under the band scales the radiator drive out of the model."""
        raw = {"tau_room_min": 240.0, "gain_heater": 0.4}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_heater_gain_above_the_band_is_rejected(self):
        """A gain over the band is outside what the fit itself can emit."""
        raw = {"tau_room_min": 240.0, "gain_heater": 5.1}
        assert deserialize_mpc_v2_reid(raw) is None

    @pytest.mark.parametrize(
        ("tau_room_min", "gain_heater"),
        [
            (TAU_ROOM_BOUNDS_MIN[0], GAIN_HEATER_BOUNDS[0]),
            (TAU_ROOM_BOUNDS_MIN[1], GAIN_HEATER_BOUNDS[1]),
        ],
    )
    def test_band_edges_are_kept(self, tau_room_min, gain_heater):
        """The band is inclusive, so a value the fit can emit still restores."""
        raw = {"tau_room_min": tau_room_min, "gain_heater": gain_heater}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.tau_room_min == tau_room_min
        assert reid.gain_heater == gain_heater

    def test_out_of_band_entry_is_absent_after_a_full_load(self):
        """The rejected entry leaves no key behind and spares its neighbour."""
        raw = _serialize(RuntimeState())
        raw["mpc_v2_reid"] = {
            "good": {"tau_room_min": 240.0, "gain_heater": 3.0},
            "bad": {"tau_room_min": 5e-324, "gain_heater": 3.0},
        }
        restored = _deserialize(raw)
        assert "bad" not in restored.mpc_v2_reid
        assert restored.mpc_v2_reid["good"].tau_room_min == 240.0

    def test_infinite_segment_count_falls_back_to_zero(self):
        """An unconvertible segment count must not abort the whole load."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": float("inf")}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.n_segments == 0

    def test_unstorable_segment_count_falls_back_to_zero(self):
        """A count wider than 64 bits could never be written back."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": 1e300}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.n_segments == 0

    def test_negative_segment_count_falls_back_to_zero(self):
        """Segments are counted, so a negative tally is not a count."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": -4}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.n_segments == 0

    def test_largest_storable_segment_count_is_kept(self):
        """The bound is inclusive: the widest storable count still passes."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": _MAX_STORED_INT}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.n_segments == _MAX_STORED_INT

    def test_string_nan_discards_the_entry(self):
        """A JSON string is the route a real store file can deliver a NaN by."""
        raw = {"tau_room_min": "NaN", "gain_heater": 3.0}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_string_infinity_discards_the_entry(self):
        """``float()`` accepts the spelling ``Infinity``, so the guard must too."""
        raw = {"tau_room_min": 240.0, "gain_heater": "Infinity"}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_string_overflowing_exponent_discards_the_entry(self):
        """``float("1e999")`` is infinity, so the entry is just as corrupt."""
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "rmse_prior_K": "1e999"}
        assert deserialize_mpc_v2_reid(raw) is None

    def test_non_finite_string_survives_a_real_store_read(self):
        """The JSON parser keeps ``"NaN"`` a string, so it reaches the guard.

        A bare ``NaN`` literal never gets this far — the parser rejects it
        and Home Assistant quarantines the file — but a quoted one parses
        cleanly and only ``float()`` turns it into a non-finite number.
        """
        raw = json_loads(
            '{"version": 1, "mpc_v2_reid": {"bt:reid": '
            '{"tau_room_min": "NaN", "gain_heater": 3.0}}}'
        )
        assert isinstance(raw, dict)
        assert raw["mpc_v2_reid"]["bt:reid"]["tau_room_min"] == "NaN"
        assert _deserialize(raw).mpc_v2_reid == {}

    def test_oversized_stored_count_cannot_break_the_next_save(self):
        """A store file may carry a number that JSON parses as a huge float.

        ``int()`` of it yields an integer the store's encoder refuses, which
        would abort every later save for this config entry.
        """
        raw = json_loads(
            '{"version": 1, "mpc_v2_reid": {"bt:reid": {"tau_room_min": 240.0, '
            '"gain_heater": 3.0, "n_segments": ' + "9" * 300 + "}}}"
        )
        assert isinstance(raw, dict)
        assert isinstance(raw["mpc_v2_reid"]["bt:reid"]["n_segments"], float)
        restored = _deserialize(raw)
        assert restored.mpc_v2_reid["bt:reid"].n_segments == 0
        prepare_save_json(_serialize(restored))

    def test_poisoned_entry_is_absent_after_a_full_load(self):
        """A discarded entry leaves no key behind for the prior lookup."""
        raw = _serialize(RuntimeState())
        raw["mpc_v2_reid"] = {
            "good": {"tau_room_min": 240.0, "gain_heater": 3.0},
            "bad": {"tau_room_min": float("nan"), "gain_heater": 3.0},
        }
        restored = _deserialize(raw)
        assert "bad" not in restored.mpc_v2_reid
        assert restored.mpc_v2_reid["good"].tau_room_min == 240.0


class TestStorableIntegerBound:
    """The accepted integer range must be the one the store's encoder writes."""

    def test_bounds_are_exactly_what_the_encoder_accepts(self):
        """Both bounds are storable and one step past either one is not."""
        json_bytes({"n": _MIN_STORED_INT})
        json_bytes({"n": _MAX_STORED_INT})
        with pytest.raises(TypeError):
            json_bytes({"n": _MIN_STORED_INT - 1})
        with pytest.raises(TypeError):
            json_bytes({"n": _MAX_STORED_INT + 1})


class TestStoredIntegerFields:
    """Restored integer fields must stay writable by the store's encoder."""

    def test_oversized_count_keeps_the_default(self):
        """An unstorable tally restores as 0, the field's own default."""
        mpc = deserialize_mpc({"gain_est": 0.5, "dead_zone_hits": float(2**70)})
        assert mpc.dead_zone_hits == 0
        assert mpc.gain_est == 0.5

    def test_negative_count_keeps_the_default(self):
        """Occurrences are tallied upwards, so a negative value is not one."""
        assert deserialize_mpc({"loss_learn_count": -7}).loss_learn_count == 0

    def test_largest_storable_count_is_kept(self):
        """The bound is inclusive, so the widest storable tally survives."""
        raw = {"profile_samples": _MAX_STORED_INT}
        assert deserialize_mpc(raw).profile_samples == _MAX_STORED_INT

    def test_oversized_sign_keeps_the_default(self):
        """An unstorable direction is dropped like any other unusable field."""
        assert (
            deserialize_pid({"last_error_sign": float(2**70)}).last_error_sign is None
        )

    def test_negative_sign_is_kept(self):
        """A direction is signed, so the count rule must not reach it."""
        assert deserialize_pid({"last_delta_sign": -1}).last_delta_sign == -1

    def test_oversized_count_cannot_break_the_next_save(self):
        """A store number too wide for JSON must not disable persistence.

        The parser hands it over as a float; ``int()`` of it yields an
        integer the encoder refuses, and the Store only logs that failure,
        so the entry's state file would silently stop being written.
        """
        raw = json_loads(
            '{"version": 1, "mpc": {"k": {"dead_zone_hits": '
            + "9" * 300
            + '}}, "pid": {"k": {"last_error_sign": '
            + "9" * 300
            + "}}}"
        )
        assert isinstance(raw, dict)
        restored = _deserialize(raw)
        assert restored.mpc["k"].dead_zone_hits == 0
        assert restored.pid["k"].last_error_sign is None
        prepare_save_json(_serialize(restored))


class TestNonFiniteStringsFromAStore:
    """``float()`` accepts spellings the JSON parser leaves as strings."""

    def test_mpc_string_nan_resets_the_entry(self):
        """A quoted NaN reaches the guard and the entry restarts from defaults."""
        mpc = deserialize_mpc({"gain_est": 0.5, "loss_est": "NaN"})
        assert mpc == MpcState()

    def test_pid_string_infinity_resets_the_entry(self):
        """The spelling ``Infinity`` is a float to Python, not a wrong type."""
        pid = deserialize_pid({"pid_integral": 1.5, "pid_kp": "Infinity"})
        assert pid == PIDState()

    def test_tpi_string_overflowing_exponent_resets_the_entry(self):
        """``float("1e999")`` overflows to infinity and poisons the entry."""
        tpi = deserialize_tpi({"last_percent": "1e999"})
        assert tpi == TpiState()

    def test_unparsable_string_still_only_skips_the_field(self):
        """Only strings that parse as non-finite floats reject an entry."""
        mpc = deserialize_mpc({"gain_est": 0.5, "loss_est": "later"})
        assert mpc.gain_est == 0.5


class TestNullableFieldSets:
    """Which fields may hold a stored null is read off the dataclasses."""

    def test_fields_declared_with_none_are_nullable(self):
        """A ``| None`` field keeps its stored null as a value."""
        assert "gain_est" in _MPC_NULLABLE_FIELDS
        assert "last_percent" in _MPC_V2_NULLABLE_FIELDS
        assert "pid_kp" in _PID_NULLABLE_FIELDS
        assert "last_delta_sign" in _PID_NULLABLE_FIELDS
        assert "last_percent" in _TPI_NULLABLE_FIELDS

    def test_fields_declared_without_none_are_not_nullable(self):
        """A field typed ``float``, ``int``, ``str`` or a collection is not."""
        assert "u_integral" not in _MPC_NULLABLE_FIELDS
        assert "trv_profile" not in _MPC_NULLABLE_FIELDS
        assert "perf_curve" not in _MPC_NULLABLE_FIELDS
        assert "recent_errors" not in _MPC_NULLABLE_FIELDS
        assert "created_ts" not in _MPC_V2_NULLABLE_FIELDS
        assert "pid_integral" not in _PID_NULLABLE_FIELDS
        assert "last_update_ts" not in _TPI_NULLABLE_FIELDS

    def test_the_reid_result_declares_no_nullable_field(self):
        """Every persisted re-ID field is a plain number, so none takes a null."""
        assert _MPC_V2_REID_NULLABLE_FIELDS == frozenset()


class TestStoredNulls:
    """A null is a value only where the field's own type allows one.

    The store's encoder writes NaN and infinity as ``null``, so a null in
    a field typed without ``None`` is a non-finite number coming back and
    gets the same disposal as one that survived as a string.
    """

    def test_mpc_null_float_resets_the_entry(self):
        """``u_integral`` is typed ``float``, so its null is a saved NaN."""
        mpc = deserialize_mpc({"gain_est": 0.5, "u_integral": None})
        assert mpc == MpcState()

    def test_mpc_null_profile_resets_the_entry(self):
        """A null cannot be a profile name either."""
        assert deserialize_mpc({"gain_est": 0.5, "trv_profile": None}) == MpcState()

    def test_mpc_null_collection_resets_the_entry(self):
        """The collection fields are not typed to hold a null either."""
        assert deserialize_mpc({"perf_curve": None}) == MpcState()
        assert deserialize_mpc({"recent_errors": None}) == MpcState()

    def test_mpc_null_optional_field_is_restored(self):
        """A ``float | None`` field still restores its stored null."""
        mpc = deserialize_mpc({"gain_est": None, "u_integral": 3.0})
        assert mpc.gain_est is None
        assert mpc.u_integral == 3.0

    def test_pid_null_integral_resets_the_entry(self):
        """``pid_integral`` is typed ``float``; a null there is corrupt math."""
        pid = deserialize_pid({"pid_kp": 60.0, "pid_integral": None})
        assert pid == PIDState()

    def test_pid_null_sign_field_is_restored(self):
        """``last_delta_sign`` is typed ``int | None``, so its null is a value."""
        pid = deserialize_pid({"last_delta_sign": None, "pid_integral": 2.0})
        assert pid.last_delta_sign is None
        assert pid.pid_integral == 2.0

    def test_tpi_null_timestamp_resets_the_entry(self):
        """``last_update_ts`` is typed ``float``, so it cannot hold a null."""
        assert deserialize_tpi({"last_percent": 30.0, "last_update_ts": None}) == (
            TpiState()
        )

    def test_tpi_null_percent_is_restored(self):
        """``last_percent`` is typed ``float | None`` and keeps its null."""
        tpi = deserialize_tpi({"last_percent": None, "last_update_ts": 5.0})
        assert tpi.last_percent is None
        assert tpi.last_update_ts == 5.0


class TestStoredCollectionElements:
    """A collection field's own numbers are guarded like the numeric fields.

    ``perf_curve`` and ``recent_errors`` are restored as collections, so
    the field guards say nothing about what is inside them. Both are
    declared to hold numbers, which makes a null among those numbers the
    same saved NaN a null in a numeric field is.
    """

    def test_null_error_sample_resets_the_entry(self):
        """``recent_errors`` is a ``deque[float]``, so a null in it is a NaN."""
        assert deserialize_mpc({"gain_est": 0.5, "recent_errors": [0.1, None]}) == (
            MpcState()
        )

    def test_null_bin_statistic_resets_the_entry(self):
        """A bin's statistics are declared as numbers, so a null is a NaN."""
        raw = {"perf_curve": {"p00_05": {"count": 3, "avg_room_rate": None}}}
        assert deserialize_mpc(raw) == MpcState()

    def test_null_outside_the_deque_window_still_resets_the_entry(self):
        """Every stored sample is parsed, not only the last twenty kept."""
        raw = {"recent_errors": [None] + [float(i) for i in range(25)]}
        assert deserialize_mpc(raw) == MpcState()

    def test_non_finite_error_sample_resets_the_entry(self):
        """A NaN that reached the file as a string poisons the entry too."""
        assert deserialize_mpc({"recent_errors": [0.1, "NaN"]}) == MpcState()

    def test_non_finite_bin_statistic_resets_the_entry(self):
        """An infinite average makes every later average over it useless."""
        raw = {"perf_curve": {"p00_05": {"avg_percent": float("inf")}}}
        assert deserialize_mpc(raw) == MpcState()

    def test_unparsable_error_sample_only_skips_the_field(self):
        """A wrong type is schema drift, so the rest of the entry survives."""
        mpc = deserialize_mpc({"gain_est": 0.5, "recent_errors": [0.1, "later"]})
        assert mpc.gain_est == 0.5
        assert list(mpc.recent_errors) == []

    def test_bin_that_is_not_a_mapping_only_skips_the_field(self):
        """A curve whose bins are not statistic mappings costs only itself."""
        mpc = deserialize_mpc({"gain_est": 0.5, "perf_curve": {"p00_05": 3.0}})
        assert mpc.gain_est == 0.5
        assert mpc.perf_curve == {}

    def test_finite_collections_are_restored(self):
        """The element guard must not cost a healthy curve or error series."""
        raw = {
            "recent_errors": [0.1, -0.2],
            "perf_curve": {"p00_05": {"count": 3, "avg_room_rate": 0.05}},
        }
        mpc = deserialize_mpc(raw)
        assert list(mpc.recent_errors) == [0.1, -0.2]
        assert mpc.perf_curve == {"p00_05": {"count": 3, "avg_room_rate": 0.05}}

    def test_restored_error_series_keeps_its_window(self):
        """The deque's bound is unchanged by parsing its elements."""
        mpc = deserialize_mpc({"recent_errors": [float(i) for i in range(30)]})
        assert mpc.recent_errors.maxlen == 20
        assert list(mpc.recent_errors) == [float(i) for i in range(10, 30)]


class TestLiveNonFiniteRoundTrip:
    """A non-finite value held at save time must not return as ``None``."""

    async def test_saved_nan_does_not_restore_as_none(self, hass, hass_storage):
        """Save a live NaN, load it back, and check what the entry holds.

        The encoder writes the NaN as ``null``. Restoring that null into a
        field typed ``float`` leaves the entry holding ``None`` where the
        calibrator expects a number, and the first arithmetic on it raises
        ``TypeError`` — inside ``sanitize_pid_state``, before the guards
        that would have healed a NaN ever run.
        """
        manager = StateManager(hass, "nan_entry")
        pid = manager.get_pid("k")
        pid.pid_integral = float("nan")
        pid.pid_kp = 60.0
        await manager.save()

        stored = hass_storage["better_thermostat_nan_entry_state"]["data"]
        assert stored["pid"]["k"]["pid_integral"] is None

        reloaded = StateManager(hass, "nan_entry")
        await reloaded.load()
        restored = reloaded.state.pid["k"]
        assert restored == PIDState()
        assert restored.pid_integral + 1.0 == 1.0

    async def test_saved_nan_inside_a_collection_does_not_restore_as_none(
        self, hass, hass_storage
    ):
        """The same route reaches the numbers inside an MPC collection.

        The encoder writes them as ``null`` in the stored list and in the
        stored bin, where no field is null and so no field guard applies.
        Copied back verbatim they would leave a ``None`` among numbers that
        ``sanitize_mpc_state`` grades healthy — its finiteness walk has no
        number to reject — and the first sum over the series raises
        ``TypeError``.
        """
        manager = StateManager(hass, "nan_collection")
        mpc = manager.get_mpc("k")
        mpc.gain_est = 0.5
        mpc.recent_errors = deque([0.1, float("nan")], maxlen=20)
        mpc.perf_curve = {"p00_05": {"count": 3, "avg_room_rate": float("nan")}}
        await manager.save()

        stored = hass_storage["better_thermostat_nan_collection_state"]["data"]
        assert stored["mpc"]["k"]["recent_errors"] == [0.1, None]
        assert stored["mpc"]["k"]["perf_curve"]["p00_05"]["avg_room_rate"] is None

        reloaded = StateManager(hass, "nan_collection")
        await reloaded.load()
        restored = reloaded.state.mpc["k"]
        assert restored == MpcState()
        assert sum(restored.recent_errors) == 0.0


class TestDeserializeMpcV2:
    """MPC v2 entries obey the same finiteness contract as their siblings."""

    def test_finite_payload_is_restored(self):
        """A plausible payload keeps every field, snapshot included."""
        raw = {
            "last_percent": 42.0,
            "last_compute_ts": 100.0,
            "created_ts": 10.0,
            "outdoor_fallback_logged": True,
            "snapshot": {"u_prev": 0.5},
        }
        state = deserialize_mpc_v2(raw)
        assert state.last_percent == 42.0
        assert state.last_compute_ts == 100.0
        assert state.created_ts == 10.0
        assert state.outdoor_fallback_logged is True
        assert state.snapshot == {"u_prev": 0.5}

    def test_non_finite_strings_from_a_store_discard_the_entry(self):
        """A stored file delivers non-finite numbers as JSON strings."""
        raw = json_loads(
            '{"version":1,"mpc_v2":{"k1":{"last_percent":"NaN",'
            '"last_compute_ts":"1e999","created_ts":"-1e999"}}}'
        )
        assert isinstance(raw, dict)
        assert "k1" not in _deserialize(raw).mpc_v2

    def test_poisoned_entry_drops_the_snapshot(self):
        """The snapshot came from the controller whose timestamp went corrupt."""
        raw = {"last_compute_ts": float("inf"), "snapshot": {"u_prev": 0.5}}
        assert deserialize_mpc_v2(raw) is None

    def test_null_timestamp_discards_the_entry(self):
        """``created_ts`` is typed ``float``, so its null is a saved NaN."""
        raw = {"created_ts": None, "snapshot": {"u_prev": 0.5}}
        assert deserialize_mpc_v2(raw) is None

    def test_null_percent_is_restored(self):
        """``last_percent`` is typed ``float | None`` and keeps its null."""
        state = deserialize_mpc_v2({"last_percent": None, "created_ts": 10.0})
        assert state is not None
        assert state.last_percent is None
        assert state.created_ts == 10.0

    def test_absent_field_keeps_its_default(self):
        """A field the payload never carried is not a null."""
        state = deserialize_mpc_v2({"snapshot": {"u_prev": 0.5}})
        assert state == MpcV2StateData(snapshot={"u_prev": 0.5})

    def test_wrong_type_only_skips_the_field(self):
        """A wrong type is schema drift, not corrupt math: keep the entry."""
        state = deserialize_mpc_v2({"created_ts": "later", "last_compute_ts": 100.0})
        assert state is not None
        assert state.created_ts == 0.0
        assert state.last_compute_ts == 100.0

    @pytest.mark.asyncio
    async def test_a_value_the_store_lost_is_named_on_the_way_in(self, caplog):
        """The load path is where an unreadable field is still recognisable.

        Past this point the field carries the default a first start leaves
        there, and a value the store lost looks exactly like one it never
        held. The rehydration downstream is handed the default and has
        nothing left to report.
        """
        mock_hass = AsyncMock()
        mock_store = AsyncMock()
        mock_store.async_load.return_value = {
            "version": 1,
            "mpc_v2": {"uid:climate.hall:t21.0": {"last_compute_ts": "bad"}},
        }
        with patch(f"{_SM}.Store", return_value=mock_store):
            mgr = StateManager(mock_hass, "unreadable_field")
        with caplog.at_level(logging.DEBUG):
            await mgr.load()

        reports = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(reports) == 1
        assert "last_compute_ts" in reports[0].getMessage()
        assert "uid:climate.hall:t21.0" in reports[0].getMessage()
        # The entry survives; only the field it could not read stays default.
        assert mgr.state.mpc_v2["uid:climate.hall:t21.0"].last_compute_ts == 0.0

    @pytest.mark.asyncio
    async def test_a_poisoned_key_comes_back_without_a_controller(self):
        """Dropping the key is what makes the restart a cold one.

        An entry kept with an empty ``snapshot`` would still be rehydrated
        into a controller, which then counts as initialised and skips
        seeding its estimate from the first measurement.
        """
        mock_hass = AsyncMock()
        mock_store = AsyncMock()
        mock_store.async_load.return_value = {
            "version": 1,
            "mpc_v2": {
                "k1": {
                    "last_compute_ts": "NaN",
                    "snapshot": {"v": 1, "x_hat": [18.0, 30.0], "last_u": 0.7},
                }
            },
        }
        with patch(f"{_SM}.Store", return_value=mock_store):
            mgr = StateManager(mock_hass, "poisoned_entry")
        await mgr.load()

        assert mgr.state.mpc_v2 == {}
        assert mgr.get_mpc_v2_live("k1", MpcV2Params()).controller is None


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
    async def test_load_survives_a_poisoned_store(self):
        """A store that breaks deserialization yields defaults, not a crash.

        load() runs inside the entity's startup task; an exception here
        would kill startup over data that relearning replaces anyway.
        """
        mgr, mock_store = self._make_manager_with_store()
        mock_store.async_load.return_value = {"version": 1, "mpc": {"k": {}}}
        with (
            patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")),
            patch(f"{_SM}.Store", return_value=AsyncMock()),
        ):
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
        }

        await mgr.load()

        assert mgr.state.mpc["k1"].gain_est == 0.5
        assert mgr.state.mpc["k1"].dead_zone_hits == 2
        assert mgr.state.thermal.heating_power == 1000.0
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
# StateManager — delayed (coalesced) save
# ---------------------------------------------------------------------------


class _StubMpcV2Controller:
    """Minimal stand-in exposing the export surface the save path uses."""

    def __init__(self, snapshot: ControllerSnapshot) -> None:
        self.snapshot = snapshot

    def export_snapshot(self) -> ControllerSnapshot:
        return self.snapshot


def _make_snapshot(last_u: float) -> ControllerSnapshot:
    return ControllerSnapshot(
        v=1,
        x_hat=[19.0, 19.0],
        kalman_P=[[1.0, 0.0], [0.0, 1.0]],
        D_hat_K_per_min=0.0,
        last_u=last_u,
        e_integral_K_min=0.0,
        u_history=[],
        rg_v_C=None,
        last_t_s=0.0,
        next_mpc_t_s=0.0,
    )


class TestScheduleDelaySave:
    """The coalesced save path serializes the live state at write time."""

    def _make_manager_with_store(self):
        mock_hass = AsyncMock()
        mock_store = AsyncMock()
        mock_store.async_delay_save = MagicMock()
        with patch(
            "custom_components.better_thermostat.utils.state_manager.Store",
            return_value=mock_store,
        ):
            mgr = StateManager(mock_hass, "test_entry")
        return mgr, mock_store

    def test_delay_save_serializes_current_live_mpc_v2_state(self):
        """The write-time payload reflects the live MPC v2 controller state."""
        mgr, mock_store = self._make_manager_with_store()
        live = mgr.get_mpc_v2_live("k1", MpcV2Params())
        live.controller = _StubMpcV2Controller(_make_snapshot(last_u=10.0))
        live.last_percent = 42.0
        mgr.set_mpc_v2_live("k1", live)

        mgr.schedule_delay_save()
        # Mutations after scheduling must still land in the payload:
        # serialization happens when the Store fires the delayed write.
        live.last_percent = 55.0
        live.controller.snapshot = _make_snapshot(last_u=77.0)

        data_func = mock_store.async_delay_save.call_args[0][0]
        data = data_func()

        assert data["mpc_v2"]["k1"]["last_percent"] == 55.0
        assert data["mpc_v2"]["k1"]["snapshot"]["last_u"] == 77.0
        assert mgr.dirty is False

    def test_delay_save_keeps_the_last_good_entry_when_the_export_is_corrupt(self):
        """A live controller gone non-finite must not overwrite what is stored."""
        mgr, mock_store = self._make_manager_with_store()
        live = mgr.get_mpc_v2_live("k1", MpcV2Params())
        live.controller = _StubMpcV2Controller(_make_snapshot(last_u=10.0))
        live.last_percent = 42.0
        live.last_compute_ts = 100.0
        mgr.set_mpc_v2_live("k1", live)
        mgr.schedule_delay_save()
        mock_store.async_delay_save.call_args[0][0]()

        live.last_compute_ts = float("nan")
        mgr.schedule_delay_save()
        data = mock_store.async_delay_save.call_args[0][0]()

        assert data["mpc_v2"]["k1"]["last_compute_ts"] == 100.0

    def test_delay_save_keeps_dirty_when_pre_save_fails(self):
        """A failing pre-save leaves the manager dirty for a retry."""
        mgr, mock_store = self._make_manager_with_store()
        mgr.mark_dirty()

        def _boom():
            raise RuntimeError("pre-save failed")

        mgr.schedule_delay_save(pre_save=_boom)
        data_func = mock_store.async_delay_save.call_args[0][0]
        data = data_func()

        assert isinstance(data, dict)
        assert mgr.dirty is True

    def test_delay_save_keeps_dirty_when_live_sync_fails(self):
        """A failing MPC v2 live-state sync leaves the manager dirty."""
        mgr, mock_store = self._make_manager_with_store()
        mgr.mark_dirty()

        mgr.schedule_delay_save()
        data_func = mock_store.async_delay_save.call_args[0][0]
        with patch.object(
            mgr, "_sync_mpc_v2_live", side_effect=RuntimeError("sync failed")
        ):
            data = data_func()

        assert isinstance(data, dict)
        assert mgr.dirty is True


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

    def test_non_finite_values_yield_none(self):
        """NaN/inf persisted thermal stats degrade to None instead of leaking."""
        mgr = _make_manager()
        mgr.thermal = ThermalStats(
            heating_power=float("nan"), heat_loss_rate=float("inf")
        )
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


# ---------------------------------------------------------------------------
# Filter state persistence
# ---------------------------------------------------------------------------


class TestFilterState:
    """The runtime filter state persists through the unified store."""

    def test_record_and_read_back(self):
        """record_filters stores the values and marks dirty."""
        mgr = _make_manager()
        mgr.record_filters(20.5, 0.0012)
        assert mgr.filters.external_temp_ema == 20.5
        assert mgr.filters.temp_slope == 0.0012
        assert mgr.dirty is True

    def test_roundtrip_through_serialization(self):
        """Filter values survive a serialize/deserialize cycle."""
        mgr = _make_manager()
        mgr.record_filters(20.5, 0.0012)
        restored = _deserialize(_serialize(mgr.state))
        assert restored.filters.external_temp_ema == 20.5
        assert restored.filters.temp_slope == 0.0012

    def test_non_finite_values_are_dropped_on_load(self):
        """Poisoned filter values degrade to defaults instead of loading."""
        raw = _serialize(RuntimeState())
        raw["filters"] = {"external_temp_ema": float("nan"), "temp_slope": "oops"}
        restored = _deserialize(raw)
        assert restored.filters.external_temp_ema is None
        assert restored.filters.temp_slope is None

    def test_non_finite_samples_are_not_recorded(self):
        """A NaN or infinite sample is dropped where it is handed in.

        The store writes both back as null, so recording one would report a
        filter value the next start silently cannot restore.
        """
        mgr = _make_manager()
        mgr.record_filters(float("nan"), float("inf"))
        assert mgr.filters.external_temp_ema is None
        assert mgr.filters.temp_slope is None

        mgr.record_filters(20.5, 0.0012)
        assert mgr.filters.external_temp_ema == 20.5
        assert mgr.filters.temp_slope == 0.0012


# ---------------------------------------------------------------------------
# Unreadable store handling
# ---------------------------------------------------------------------------

_LIVE_STORE_KEY = "better_thermostat_test_entry_state"
_SET_ASIDE_KEY = "better_thermostat_test_entry_state.corrupt"


@contextmanager
def _stores_by_key():
    """Patch Store so every construction is recorded under its storage key.

    A store nobody has written yet loads as ``None``, which is what Home
    Assistant returns for a missing storage file.
    """
    stores: dict[str, AsyncMock] = {}

    def _store_for(_hass, _version, key):
        if key not in stores:
            store = AsyncMock()
            store.async_load = AsyncMock(return_value=None)
            stores[key] = store
        return stores[key]

    with patch(f"{_SM}.Store", side_effect=_store_for):
        yield stores


class TestUnreadableStoreIsKeptForRecovery:
    """An unreadable store is copied aside before defaults take its place.

    Falling back to defaults is deliberate -- startup must not die over a
    damaged file -- but the next save writes those defaults over the live
    store, so without a copy the only record of what an installation had
    learned is gone.
    """

    @pytest.mark.asyncio
    async def test_copy_carries_the_content_that_could_not_be_read(self):
        """The set-aside copy holds the payload verbatim."""
        with _stores_by_key() as stores:
            mgr = StateManager(AsyncMock(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": 1,
                "mpc": {"k1": {"gain_est": 0.5}},
            }
            with patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")):
                await mgr.load()

        copy = stores[_SET_ASIDE_KEY]
        copy.async_save.assert_awaited_once()
        assert copy.async_save.await_args[0][0] == {
            "version": 1,
            "mpc": {"k1": {"gain_est": 0.5}},
        }

    @pytest.mark.asyncio
    async def test_defaults_still_replace_the_unreadable_state(self):
        """Setting the copy aside does not change the fallback behaviour."""
        with _stores_by_key() as stores:
            mgr = StateManager(AsyncMock(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {"version": 1, "mpc": {}}
            with patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")):
                await mgr.load()

        assert mgr.state.mpc == {}
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_an_earlier_copy_is_not_overwritten(self):
        """The first copy is the one still holding the accumulated state."""
        with _stores_by_key() as stores:
            mgr = StateManager(AsyncMock(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {"version": 1, "mpc": {}}
            earlier = AsyncMock()
            earlier.async_load = AsyncMock(
                return_value={"version": 1, "mpc": {"k1": {"gain_est": 0.9}}}
            )
            stores[_SET_ASIDE_KEY] = earlier
            with patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")):
                await mgr.load()

        earlier.async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_readable_store_leaves_no_copy(self):
        """Nothing is set aside when the store deserializes."""
        with _stores_by_key() as stores:
            mgr = StateManager(AsyncMock(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": 1,
                "mpc": {"k1": {"gain_est": 0.5}},
            }
            await mgr.load()

        assert _SET_ASIDE_KEY not in stores
        assert mgr.state.mpc["k1"].gain_est == 0.5

    @pytest.mark.asyncio
    async def test_an_empty_store_leaves_no_copy(self):
        """A first start has nothing to preserve."""
        with _stores_by_key() as stores:
            mgr = StateManager(AsyncMock(), "test_entry")
            await mgr.load()

        assert _SET_ASIDE_KEY not in stores

    @pytest.mark.asyncio
    async def test_a_failed_copy_still_lets_startup_continue(self, caplog):
        """A storage error while copying is reported, not raised."""
        with _stores_by_key() as stores:
            mgr = StateManager(AsyncMock(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {"version": 1, "mpc": {}}
            with (
                caplog.at_level(logging.WARNING, logger=_SM),
                patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")),
            ):
                stores[_SET_ASIDE_KEY] = AsyncMock()
                stores[_SET_ASIDE_KEY].async_load = AsyncMock(return_value=None)
                stores[_SET_ASIDE_KEY].async_save = AsyncMock(
                    side_effect=HomeAssistantError("disk full")
                )
                await mgr.load()

        assert mgr.state.mpc == {}
        assert "could not set the unreadable state aside" in caplog.text

    @pytest.mark.asyncio
    async def test_removing_the_entry_removes_the_copy_too(self):
        """A deleted config entry leaves neither file behind."""
        with _stores_by_key() as stores:
            await StateManager.async_remove_store(AsyncMock(), "test_entry")

        stores[_LIVE_STORE_KEY].async_remove.assert_awaited_once()
        stores[_SET_ASIDE_KEY].async_remove.assert_awaited_once()
