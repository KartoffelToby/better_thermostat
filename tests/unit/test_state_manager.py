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
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import timedelta
import logging
from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CoreState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import storage
from homeassistant.helpers.json import json_bytes, prepare_save_json
from homeassistant.util import dt as dt_util
from homeassistant.util.file import WriteError
from homeassistant.util.json import json_loads
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Params,
    MpcV2State,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    ControllerSnapshot,
    MpcV2Controller,
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
    _STORED_MPC_KEYS,
    _STORED_MPC_V2_REID_KEYS,
    CURRENT_VERSION,
    FilterState,
    MpcState,
    MpcV2ReidData,
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
    thermostat_of_key,
)
from custom_components.better_thermostat.utils.stored_values import (
    MAX_STORED_INT,
    MIN_STORED_INT,
)

_SM = "custom_components.better_thermostat.utils.state_manager"


def _nullable_fields(cls: type) -> frozenset[str]:
    """Return the names of *cls*'s fields whose declared type admits ``None``.

    Read off the declarations rather than listed by hand, so the set
    still matches once a field's type changes.
    """
    hints = get_type_hints(cls)
    return frozenset(
        f.name for f in fields(cls) if type(None) in get_args(hints[f.name])
    )


_MPC_NULLABLE_FIELDS = _nullable_fields(MpcState)
_MPC_V2_NULLABLE_FIELDS = _nullable_fields(MpcV2StateData)
_MPC_V2_REID_NULLABLE_FIELDS = _nullable_fields(MpcV2ReidData)
_PID_NULLABLE_FIELDS = _nullable_fields(PIDState)
_TPI_NULLABLE_FIELDS = _nullable_fields(TpiState)


def _edited_payload() -> dict[str, object]:
    """Return a freshly serialized payload, open to edits the way a store file is."""
    return dict(_serialize(RuntimeState()))


def _stored_section(
    payload: Mapping[str, object], section: str
) -> Mapping[str, object]:
    """Return one section of a stored payload."""
    entries = payload[section]
    assert isinstance(entries, dict)
    return entries


def _stored_entry(
    payload: Mapping[str, object], section: str, key: str
) -> Mapping[str, object]:
    """Return one keyed entry of a stored payload's section."""
    entry = _stored_section(payload, section)[key]
    assert isinstance(entry, dict)
    return entry


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

    def test_filters_keep_their_stored_keys(self):
        """The room temperature EMA is stored as ``external_temp_ema``."""
        original = RuntimeState(
            filters=FilterState(room_temperature_ema=20.4, temperature_slope=0.002)
        )

        raw = _serialize(original)
        restored = _deserialize(raw)

        assert raw["filters"] == {"external_temp_ema": 20.4, "temp_slope": 0.002}
        assert restored.filters.room_temperature_ema == 20.4
        assert restored.filters.temperature_slope == 0.002

    def test_reid_results_keep_their_stored_keys(self):
        """The re-identification RMSEs are stored as ``rmse_prior_K``/``rmse_fit_K``."""
        original = RuntimeState(
            mpc_v2_reid={
                "k1": MpcV2ReidData(
                    tau_room_minutes=240.0,
                    gain_heater=3.0,
                    rmse_prior_kelvin=0.4,
                    rmse_fit_kelvin=0.1,
                )
            }
        )

        raw = _serialize(original)
        restored = _deserialize(raw)

        stored = raw["mpc_v2_reid"]["k1"]
        assert (stored["rmse_prior_K"], stored["rmse_fit_K"]) == (0.4, 0.1)
        assert "rmse_prior_kelvin" not in stored
        assert "rmse_fit_kelvin" not in stored
        assert restored.mpc_v2_reid["k1"] == original.mpc_v2_reid["k1"]

    def test_mpc_temperatures_keep_their_stored_keys(self):
        """The MPC v1 temperatures are stored under their 1.9 keys."""
        original = RuntimeState(
            mpc={
                "k1": MpcState(
                    last_target_temperature=21.0,
                    last_sensor_temperature=20.5,
                    last_room_temperature=20.25,
                )
            }
        )

        raw = _serialize(original)
        restored = _deserialize(raw)

        stored = raw["mpc"]["k1"]
        assert (
            stored["last_target_C"],
            stored["last_sensor_temp_C"],
            stored["last_room_temp_C"],
        ) == (21.0, 20.5, 20.25)
        assert (
            not {
                "last_target_temperature",
                "last_sensor_temperature",
                "last_room_temperature",
            }
            & stored.keys()
        )
        restored_mpc = restored.mpc["k1"]
        assert restored_mpc.last_target_temperature == 21.0
        assert restored_mpc.last_sensor_temperature == 20.5
        assert restored_mpc.last_room_temperature == 20.25

    def test_legacy_presets_section_ignored(self):
        """A legacy presets section in a stored payload is ignored.

        Preset temperatures live in the preset number entities.
        """
        raw = _edited_payload()
        raw["presets"] = {"comfort": 22.0}

        restored = _deserialize(raw)

        assert not hasattr(restored, "presets")

    def test_thermal_rejects_non_finite(self):
        """NaN/inf thermal stats in a stored payload are rejected on load."""
        raw = _edited_payload()
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


class TestDeserializeMpcFieldSpellings:
    """The temperature anchors read under their store key and their attribute."""

    def test_an_asdict_snapshot_keeps_the_anchors(self):
        """A snapshot taken with ``asdict`` restores the three anchors."""
        state = MpcState(
            last_target_temperature=21.5,
            last_sensor_temperature=20.25,
            last_room_temperature=19.75,
        )
        restored = deserialize_mpc(asdict(state))
        assert restored.last_target_temperature == 21.5
        assert restored.last_sensor_temperature == 20.25
        assert restored.last_room_temperature == 19.75

    def test_the_store_key_wins_over_the_attribute_name(self):
        """An entry carrying both spellings reads the store key."""
        raw = {"last_target_C": 22.0, "last_target_temperature": 18.0}
        assert deserialize_mpc(raw).last_target_temperature == 22.0


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
        raw: dict[str, object] = {"gain_est": None, "loss_est": None}
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
        raw: dict[str, object] = {"pid_kp": None}
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
        assert reid.tau_room_minutes == 240.0
        assert reid.gain_heater == 3.0
        assert reid.fitted_ts == 1000.0
        assert reid.rmse_prior_kelvin == 0.4
        assert reid.rmse_fit_kelvin == 0.1
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
        assert reid.tau_room_minutes == 240.0
        assert reid.rmse_fit_kelvin == 0.0

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

    def test_null_validation_metric_is_reported_under_its_stored_key(self, caplog):
        """The report names the key the file holds, as the other paths do."""
        base = {"tau_room_min": 240.0, "gain_heater": 3.0}
        with caplog.at_level(logging.WARNING, logger=_SM):
            assert deserialize_mpc_v2_reid({**base, "rmse_fit_K": None}) is None
        assert "rmse_fit_K is null" in caplog.text

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
        assert reid.rmse_prior_kelvin == 0.0
        assert reid.rmse_fit_kelvin == 0.0
        assert reid.n_segments == 0

    def test_null_entry_is_absent_after_a_full_load(self):
        """A saved NaN comes back as a null and leaves no key behind."""
        raw = _edited_payload()
        raw["mpc_v2_reid"] = {
            "good": {"tau_room_min": 240.0, "gain_heater": 3.0},
            "bad": {"tau_room_min": 240.0, "gain_heater": 3.0, "rmse_fit_K": None},
        }
        restored = _deserialize(raw)
        assert "bad" not in restored.mpc_v2_reid
        assert restored.mpc_v2_reid["good"].tau_room_minutes == 240.0

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
        ("tau_room_minutes", "gain_heater"),
        [
            (TAU_ROOM_BOUNDS_MIN[0], GAIN_HEATER_BOUNDS[0]),
            (TAU_ROOM_BOUNDS_MIN[1], GAIN_HEATER_BOUNDS[1]),
        ],
    )
    def test_band_edges_are_kept(self, tau_room_minutes, gain_heater):
        """The band is inclusive, so a value the fit can emit still restores."""
        raw = {"tau_room_min": tau_room_minutes, "gain_heater": gain_heater}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.tau_room_minutes == tau_room_minutes
        assert reid.gain_heater == gain_heater

    def test_out_of_band_entry_is_absent_after_a_full_load(self):
        """The rejected entry leaves no key behind and spares its neighbour."""
        raw = _edited_payload()
        raw["mpc_v2_reid"] = {
            "good": {"tau_room_min": 240.0, "gain_heater": 3.0},
            "bad": {"tau_room_min": 5e-324, "gain_heater": 3.0},
        }
        restored = _deserialize(raw)
        assert "bad" not in restored.mpc_v2_reid
        assert restored.mpc_v2_reid["good"].tau_room_minutes == 240.0

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
        raw = {"tau_room_min": 240.0, "gain_heater": 3.0, "n_segments": MAX_STORED_INT}
        reid = deserialize_mpc_v2_reid(raw)
        assert reid is not None
        assert reid.n_segments == MAX_STORED_INT

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
        assert _stored_entry(raw, "mpc_v2_reid", "bt:reid")["tau_room_min"] == "NaN"
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
        assert isinstance(
            _stored_entry(raw, "mpc_v2_reid", "bt:reid")["n_segments"], float
        )
        restored = _deserialize(raw)
        assert restored.mpc_v2_reid["bt:reid"].n_segments == 0
        prepare_save_json(dict(_serialize(restored)))

    def test_poisoned_entry_is_absent_after_a_full_load(self):
        """A discarded entry leaves no key behind for the prior lookup."""
        raw = _edited_payload()
        raw["mpc_v2_reid"] = {
            "good": {"tau_room_min": 240.0, "gain_heater": 3.0},
            "bad": {"tau_room_min": float("nan"), "gain_heater": 3.0},
        }
        restored = _deserialize(raw)
        assert "bad" not in restored.mpc_v2_reid
        assert restored.mpc_v2_reid["good"].tau_room_minutes == 240.0


class TestStorableIntegerBound:
    """The accepted integer range must be the one the store's encoder writes."""

    def test_bounds_are_exactly_what_the_encoder_accepts(self):
        """Both bounds are storable and one step past either one is not."""
        json_bytes({"n": MIN_STORED_INT})
        json_bytes({"n": MAX_STORED_INT})
        with pytest.raises(TypeError):
            json_bytes({"n": MIN_STORED_INT - 1})
        with pytest.raises(TypeError):
            json_bytes({"n": MAX_STORED_INT + 1})


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
        raw = {"profile_samples": MAX_STORED_INT}
        assert deserialize_mpc(raw).profile_samples == MAX_STORED_INT

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
        prepare_save_json(dict(_serialize(restored)))


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


# Each deserializer, the dataclass it builds, its fields that parse a stored
# value, and the store keys of the fields stored under another name. The
# MPC v2 entry parses three of its fields; the rest reach no guard.
_NULL_CASES = [
    (deserialize_mpc, MpcState, [f.name for f in fields(MpcState)], _STORED_MPC_KEYS),
    (
        deserialize_mpc_v2,
        MpcV2StateData,
        ["last_percent", "last_compute_ts", "created_ts"],
        dict[str, str](),
    ),
    (
        deserialize_mpc_v2_reid,
        MpcV2ReidData,
        [f.name for f in fields(MpcV2ReidData)],
        _STORED_MPC_V2_REID_KEYS,
    ),
    (deserialize_pid, PIDState, [f.name for f in fields(PIDState)], dict[str, str]()),
    (deserialize_tpi, TpiState, [f.name for f in fields(TpiState)], dict[str, str]()),
]


@pytest.mark.parametrize(
    ("deserialize", "persisted", "name", "renamed"),
    [
        (deserialize, persisted, name, renamed)
        for deserialize, persisted, names, renamed in _NULL_CASES
        for name in names
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_stored_null_is_kept_exactly_where_the_field_declares_none(
    deserialize, persisted: type, name: str, renamed: dict[str, str]
):
    """Each field's reading of a null follows its declared type.

    A ``| None`` field restores the null; every other field is handed the
    saved NaN a null stands for, and the entry starts over.
    """
    poisoned: list[str] = []
    restored = deserialize({renamed.get(name, name): None}, poisoned=poisoned)
    if name in _nullable_fields(persisted):
        assert restored is not None
        assert getattr(restored, name) is None
        assert poisoned == []
    else:
        assert poisoned != []
        assert restored in (None, persisted())


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

    def test_number_in_place_of_a_collection_only_skips_the_field(self):
        """A collection stored as a plain number is schema drift, not a NaN."""
        mpc = deserialize_mpc({"gain_est": 0.5, "perf_curve": 3, "recent_errors": 1.5})
        assert mpc.gain_est == 0.5
        assert mpc.perf_curve == {}
        assert list(mpc.recent_errors) == []

    def test_non_finite_string_in_place_of_a_collection_resets_the_entry(self):
        """A collection stored as ``"NaN"`` is the saved NaN it spells."""
        assert deserialize_mpc({"gain_est": 0.5, "perf_curve": "NaN"}) == MpcState()
        assert deserialize_mpc({"gain_est": 0.5, "recent_errors": "inf"}) == (
            MpcState()
        )

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
        assert state is not None
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
        """An mpc entry that is not a mapping leaves no key behind."""
        raw = {"version": 1, "mpc": {"key1": "not_a_dict", "key2": 42}}
        state = _deserialize(raw)
        assert "key1" not in state.mpc
        assert "key2" not in state.mpc

    def test_non_dict_thermal_ignored(self):
        """Non-dict thermal section falls through to defaults."""
        raw = {"version": 1, "thermal": "garbage"}
        state = _deserialize(raw)
        assert state.thermal.heating_power is None


_VALID_REID = {"tau_room_min": 240.0, "gain_heater": 3.0}


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
            pytest.param(deserialize_pid, {"pid_kp": "abc"}, "pid_kp", id="pid"),
            pytest.param(
                deserialize_tpi, {"last_percent": "bad"}, "last_percent", id="tpi"
            ),
            pytest.param(
                deserialize_mpc_v2_reid,
                {**_VALID_REID, "rmse_fit_K": "later"},
                "rmse_fit_K",
                id="mpc_v2_reid",
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
    def test_an_unreadable_field_is_named(self, caplog, deserialize, raw, field):
        """A field of the wrong type keeps its default and is reported by name."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            deserialize(raw)

        assert any(field in message for message in _warnings(caplog)), _warnings(caplog)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("tau_room_min", 1e300),
            ("tau_room_min", 5e-324),
            ("gain_heater", 0.4),
            ("gain_heater", 5.1),
        ],
    )
    def test_an_out_of_band_reid_result_is_named(self, caplog, field, value):
        """A stored fit outside the plausible band is refused and reported."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            assert deserialize_mpc_v2_reid({**_VALID_REID, field: value}) is None

        assert any(field in message for message in _warnings(caplog)), _warnings(caplog)

    @pytest.mark.parametrize("section", ["mpc", "mpc_v2", "mpc_v2_reid", "pid", "tpi"])
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
        "section", ["mpc", "mpc_v2", "mpc_v2_reid", "pid", "tpi", "thermal", "filters"]
    )
    def test_a_misshapen_section_is_named(self, caplog, section):
        """A whole section of the wrong shape is dropped and reported by name."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            _deserialize({"version": 1, section: "garbage"})

        assert any(section in message for message in _warnings(caplog)), _warnings(
            caplog
        )

    @pytest.mark.parametrize(
        ("section", "field", "attribute", "value"),
        [
            ("thermal", "heating_power", "heating_power", "later"),
            ("thermal", "heat_loss_rate", "heat_loss_rate", "Infinity"),
            ("filters", "external_temp_ema", "room_temperature_ema", [20.0]),
            ("filters", "temp_slope", "temperature_slope", "NaN"),
        ],
    )
    def test_an_unusable_thermal_or_filter_value_is_named(
        self, caplog, section, field, attribute, value
    ):
        """A stored thermal or filter value that is not a finite number is named."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            state = _deserialize({"version": 1, section: {field: value}})

        assert getattr(getattr(state, section), attribute) is None
        assert any(
            section in message and field in message for message in _warnings(caplog)
        ), _warnings(caplog)

    def test_a_null_thermal_or_filter_value_is_not_reported(self, caplog):
        """A null in these sections is a value not yet learned, not a lost one."""
        with caplog.at_level(logging.DEBUG, logger=_SM):
            _deserialize(
                {
                    "version": 1,
                    "thermal": {"heating_power": None, "heat_loss_rate": None},
                    "filters": {"external_temp_ema": None, "temp_slope": None},
                }
            )

        assert _warnings(caplog) == []

    @pytest.mark.parametrize(
        ("section", "entry", "field"),
        [
            ("mpc", {"gain_est": float("nan")}, "gain_est"),
            ("mpc", {"recent_errors": [0.1, None]}, "recent_errors element"),
            ("mpc_v2", {"created_ts": float("inf")}, "created_ts"),
            ("mpc_v2_reid", {**_VALID_REID, "fitted_ts": None}, "fitted_ts"),
            ("pid", {"pid_kp": float("nan")}, "pid_kp"),
            ("tpi", {"last_update_ts": float("nan")}, "last_update_ts"),
        ],
    )
    def test_a_poisoned_entry_is_named(self, caplog, section, entry, field):
        """An entry discarded for a non-finite value is reported with its key.

        Its learning starts over from defaults, so the report names the room
        the entry belonged to as well as the value that cost it.
        """
        poisoned: list[str] = []
        with caplog.at_level(logging.DEBUG, logger=_SM):
            _deserialize(
                {"version": 1, section: {"room_key": entry}}, poisoned=poisoned
            )

        assert poisoned == [f"{section}:room_key"]
        assert any(
            section in message and "room_key" in message and field in message
            for message in _warnings(caplog)
        ), _warnings(caplog)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestMigrationV0ToV1:
    """v0 to v1 migration adds missing top-level keys."""

    def test_adds_missing_keys(self):
        """Empty dict gets all required v1 keys."""
        raw: dict[str, object] = {}
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
        assert _stored_entry(result, "mpc", "k")["gain_est"] == 0.5
        assert _stored_section(result, "thermal")["heating_power"] == 1000

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
    async def test_load_survives_a_poisoned_store(self):
        """A store that breaks deserialization yields defaults, not a crash.

        load() runs inside the entity's startup task; an exception here
        would kill startup over data that relearning replaces anyway.
        """
        mgr, mock_store = self._make_manager_with_store()
        mock_store.async_load.return_value = {
            "version": 1,
            "mpc": {"k": dict[str, object]()},
        }
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
            "pid": dict[str, object](),
            "tpi": dict[str, object](),
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


def _controller_exporting(snapshot: ControllerSnapshot) -> MagicMock:
    """Return a controller double whose export answers with *snapshot*."""
    controller = create_autospec(MpcV2Controller, instance=True)
    controller.export_snapshot.return_value = snapshot
    return controller


def _make_snapshot(last_u: float) -> ControllerSnapshot:
    return ControllerSnapshot(
        v=1,
        x_hat=[19.0, 19.0],
        kalman_P=[[1.0, 0.0], [0.0, 1.0]],
        D_hat_K_per_min=0.0,
        last_u=last_u,
        e_integral_K_min=0.0,
        u_history=[],
        rg_v=None,
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
        controller = _controller_exporting(_make_snapshot(last_u=10.0))
        live.controller = controller
        live.last_percent = 42.0
        mgr.set_mpc_v2_live("k1", live)

        mgr.schedule_delay_save()
        # Mutations after scheduling must still land in the payload:
        # serialization happens when the Store fires the delayed write.
        live.last_percent = 55.0
        controller.export_snapshot.return_value = _make_snapshot(last_u=77.0)

        data_func = mock_store.async_delay_save.call_args[0][0]
        data = data_func()

        assert data["mpc_v2"]["k1"]["last_percent"] == 55.0
        assert data["mpc_v2"]["k1"]["snapshot"]["last_u"] == 77.0
        assert mgr.dirty is False

    def test_delay_save_keeps_the_last_good_entry_when_the_export_is_corrupt(self):
        """A live controller gone non-finite must not overwrite what is stored."""
        mgr, mock_store = self._make_manager_with_store()
        live = mgr.get_mpc_v2_live("k1", MpcV2Params())
        live.controller = _controller_exporting(_make_snapshot(last_u=10.0))
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

    def test_heating_power_clamped_to_maximum(self):
        """A heating_power above the max is clamped down."""
        mgr = _make_manager()
        mgr.thermal = ThermalStats(heating_power=MAX_HEATING_POWER * 10)
        hp, _ = mgr.clamped_thermal()
        assert hp == MAX_HEATING_POWER

    def test_heating_power_clamped_to_minimum(self):
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


def _manager_with_every_section(entity_id: str) -> StateManager:
    """Return a manager holding one entry for ``entity_id`` in every section."""
    mgr = _make_manager()
    key = f"uid:{entity_id}:t21.0"
    mgr.set_mpc(key, MpcState(gain_est=0.7))
    mgr.state.mpc_v2[key] = MpcV2StateData(last_percent=40.0)
    mgr.state.mpc_v2_reid[key] = MpcV2ReidData(gain_heater=0.02)
    mgr.set_pid(key, PIDState(pid_kp=77.0))
    mgr.set_tpi(key, TpiState(last_percent=35.0))
    mgr.set_mpc_v2_live(key, MpcV2State())
    mgr.get_mpc_v2_reid_runtime(key)
    return mgr


def _sections(mgr: StateManager) -> list[dict[str, object]]:
    return [
        dict(mgr.state.mpc),
        dict(mgr.state.mpc_v2),
        dict(mgr.state.mpc_v2_reid),
        dict(mgr.state.pid),
        dict(mgr.state.tpi),
        dict(mgr._mpc_v2_live),
        dict(mgr._mpc_v2_reid_live),
    ]


class TestMoveThermostat:
    """A thermostat that changed its entity id keeps what was learned for it."""

    def test_every_section_moves_to_the_new_entity_id(self):
        mgr = _manager_with_every_section("climate.old")
        mgr._dirty = False

        moved = mgr.move_thermostat("climate.old", "climate.new")

        assert moved == 7
        assert [set(section) for section in _sections(mgr)] == [
            {"uid:climate.new:t21.0"}
        ] * 7
        assert mgr.state.pid["uid:climate.new:t21.0"].pid_kp == 77.0
        assert mgr.dirty is True

    def test_another_thermostat_and_the_room_keep_their_keys(self):
        """An id that only starts like the old one is another thermostat."""
        mgr = _make_manager()
        for key in ("uid:climate.old_2:t21.0", "uid:group:t21.0", "uid:reid"):
            mgr.set_pid(key, PIDState())
        mgr._dirty = False

        moved = mgr.move_thermostat("climate.old", "climate.new")

        assert moved == 0
        assert set(mgr.state.pid) == {
            "uid:climate.old_2:t21.0",
            "uid:group:t21.0",
            "uid:reid",
        }
        assert mgr.dirty is False

    def test_the_moved_state_replaces_one_under_the_new_id(self):
        mgr = _make_manager()
        mgr.set_pid("uid:climate.old:t21.0", PIDState(pid_kp=77.0))
        mgr.set_pid("uid:climate.new:t21.0", PIDState(pid_kp=5.0))

        mgr.move_thermostat("climate.old", "climate.new")

        assert set(mgr.state.pid) == {"uid:climate.new:t21.0"}
        assert mgr.state.pid["uid:climate.new:t21.0"].pid_kp == 77.0

    def test_a_pid_loop_entry_moves_with_its_buckets(self):
        """A TRV's PID loop entry carries no bucket and moves all the same."""
        mgr = _make_manager()
        mgr.set_pid("uid:climate.old", PIDState(pid_kp=77.0, pid_integral=4.0))
        mgr.set_pid("uid:climate.old:t21.0", PIDState())
        mgr._dirty = False

        moved = mgr.move_thermostat("climate.old", "climate.new")

        assert moved == 2
        assert set(mgr.state.pid) == {"uid:climate.new", "uid:climate.new:t21.0"}
        assert mgr.state.pid["uid:climate.new"].pid_kp == 77.0
        assert mgr.state.pid["uid:climate.new"].pid_integral == 4.0
        assert mgr.dirty is True


class TestForgetThermostatsExcept:
    """State learned for a thermostat the entry no longer controls is dropped."""

    def test_every_section_drops_an_unconfigured_thermostat(self):
        mgr = _manager_with_every_section("climate.removed")
        mgr._dirty = False

        dropped = mgr.forget_thermostats_except(["climate.kept"])

        assert dropped == 7
        assert _sections(mgr) == [{}] * 7
        assert mgr.dirty is True

    def test_configured_thermostats_the_room_and_shared_keys_stay(self):
        mgr = _make_manager()
        kept = {
            "uid:climate.kept:t21.0",
            "uid:climate.kept:tunknown",
            "uid:group:t21.0",
            "uid:reid",
        }
        for key in kept:
            mgr.set_mpc(key, MpcState())
        mgr._dirty = False

        dropped = mgr.forget_thermostats_except(["climate.kept"])

        assert dropped == 0
        assert set(mgr.state.mpc) == kept
        assert mgr.dirty is False

    def test_the_pid_loop_entry_of_an_unconfigured_thermostat_is_dropped(self):
        mgr = _make_manager()
        mgr.set_pid("uid:climate.removed", PIDState(pid_kp=77.0))
        mgr.set_pid("uid:climate.kept", PIDState(pid_kp=5.0))
        mgr._dirty = False

        dropped = mgr.forget_thermostats_except(["climate.kept"])

        assert dropped == 1
        assert set(mgr.state.pid) == {"uid:climate.kept"}
        assert mgr.dirty is True


@pytest.mark.parametrize(
    ("key", "thermostat"),
    [
        ("uid:climate.trv:t21.0", "climate.trv"),
        ("uid:climate.trv:t-0.5", "climate.trv"),
        ("uid:climate.trv:tunknown", "climate.trv"),
        ("uid:group:t21.0", "group"),
        ("uid:climate.trv", "climate.trv"),
        ("uid:with:colons:climate.trv", "climate.trv"),
        ("uid:with:colons:climate.trv:t21.0", "climate.trv"),
        ("uid:reid", None),
        ("uid:group", None),
        ("uid:t21.0", None),
        ("climate.trv", None),
    ],
)
def test_thermostat_of_key_reads_every_per_thermostat_key_shape(key, thermostat):
    assert thermostat_of_key(key) == thermostat


# ---------------------------------------------------------------------------
# Filter state persistence
# ---------------------------------------------------------------------------


class TestFilterState:
    """The runtime filter state persists through the unified store."""

    def test_record_and_read_back(self):
        """record_filters stores the values and marks dirty."""
        mgr = _make_manager()
        mgr.record_filters(20.5, 0.0012)
        assert mgr.filters.room_temperature_ema == 20.5
        assert mgr.filters.temperature_slope == 0.0012
        assert mgr.dirty is True

    def test_roundtrip_through_serialization(self):
        """Filter values survive a serialize/deserialize cycle."""
        mgr = _make_manager()
        mgr.record_filters(20.5, 0.0012)
        restored = _deserialize(_serialize(mgr.state))
        assert restored.filters.room_temperature_ema == 20.5
        assert restored.filters.temperature_slope == 0.0012

    def test_non_finite_values_are_dropped_on_load(self):
        """Poisoned filter values degrade to defaults instead of loading."""
        raw = _edited_payload()
        raw["filters"] = {"external_temp_ema": float("nan"), "temp_slope": "oops"}
        restored = _deserialize(raw)
        assert restored.filters.room_temperature_ema is None
        assert restored.filters.temperature_slope is None

    def test_non_finite_samples_are_not_recorded(self):
        """A NaN or infinite sample is dropped where it is handed in.

        The store writes both back as null, so recording one would report a
        filter value the next start silently cannot restore.
        """
        mgr = _make_manager()
        mgr.record_filters(float("nan"), float("inf"))
        assert mgr.filters.room_temperature_ema is None
        assert mgr.filters.temperature_slope is None

        mgr.record_filters(20.5, 0.0012)
        assert mgr.filters.room_temperature_ema == 20.5
        assert mgr.filters.temperature_slope == 0.0012


# ---------------------------------------------------------------------------
# Unreadable store handling
# ---------------------------------------------------------------------------

_LIVE_STORE_KEY = "better_thermostat_test_entry_state"
_SET_ASIDE_KEY = "better_thermostat_test_entry_state.corrupt"


def _saved_into(store: AsyncMock):
    """Return a save side effect after which *store* loads what was saved."""

    def _save(data):
        store.async_load.return_value = data

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
            mgr = StateManager(_hass_double(), "test_entry")
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
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": 1,
                "mpc": dict[str, object](),
            }
            with patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")):
                await mgr.load()

        assert mgr.state.mpc == {}
        assert mgr.dirty is False

    @pytest.mark.asyncio
    async def test_an_earlier_copy_is_not_overwritten(self):
        """The first copy is the one still holding the accumulated state."""
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": 1,
                "mpc": dict[str, object](),
            }
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
            mgr = StateManager(_hass_double(), "test_entry")
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
            mgr = StateManager(_hass_double(), "test_entry")
            await mgr.load()

        assert _SET_ASIDE_KEY not in stores

    @pytest.mark.asyncio
    async def test_a_failed_copy_still_lets_startup_continue(self, caplog):
        """A storage error while copying is reported, not raised."""
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": 1,
                "mpc": dict[str, object](),
            }
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
    async def test_a_poisoned_entry_is_set_aside_before_it_is_reset(self):
        """A learned entry reset on load is kept aside like an unreadable store.

        The reset entry's defaults overwrite the stored one on the next save,
        which is the loss the set-aside copy exists to prevent.
        """
        payload = {
            "version": 1,
            "mpc": {"k1": {"gain_est": 0.5, "loss_est": 0.02, "kalman_P": None}},
        }
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            await mgr.load()

        assert mgr.state.mpc["k1"].gain_est is None
        assert _SET_ASIDE_KEY in stores
        stores[_SET_ASIDE_KEY].async_save.assert_awaited_once()
        assert stores[_SET_ASIDE_KEY].async_save.await_args[0][0] == payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"version": 1, "mpc": {"k1": "not_a_dict"}},
            {"version": 1, "pid": ["not", "a", "mapping"]},
            {"version": 1, "thermal": "not_a_mapping"},
            {
                "version": 1,
                "mpc_v2_reid": {"k1": {"tau_room_min": 240.0, "gain_heater": 5.1}},
            },
        ],
    )
    async def test_a_misshapen_part_is_set_aside_before_it_is_dropped(self, payload):
        """A dropped section or entry is kept aside like a poisoned one.

        That covers a section or entry of the wrong shape and a stored fit
        outside its plausible band. Their entities start from defaults, and
        those overwrite the stored payload on the next save.
        """
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            await mgr.load()

        assert _SET_ASIDE_KEY in stores
        stores[_SET_ASIDE_KEY].async_save.assert_awaited_once()
        assert stores[_SET_ASIDE_KEY].async_save.await_args[0][0] == payload

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
    @pytest.mark.parametrize(
        "poisoned_by", ["a non-finite value", "an unreadable store"]
    )
    async def test_the_live_store_is_kept_while_no_copy_exists(self, poisoned_by):
        """A save does not overwrite a payload that could not be set aside.

        The copy is taken again before the save; while it keeps failing,
        the stored payload stays the only record of what was learned, and
        the state waits unsaved.
        """
        payload = {"version": 1, "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            self._failing_copy(stores, OSError("disk full"), OSError("disk full"))
            if poisoned_by == "an unreadable store":
                with patch(f"{_SM}._deserialize", side_effect=TypeError("poisoned")):
                    await mgr.load()
            else:
                await mgr.load()
            mgr.mark_dirty()

            await mgr.save()
            mgr.schedule_delay_save()

        stores[_LIVE_STORE_KEY].async_save.assert_not_awaited()
        stores[_LIVE_STORE_KEY].async_delay_save.assert_not_called()
        assert mgr.dirty is True

    @pytest.mark.asyncio
    async def test_a_copy_that_succeeds_later_lets_the_save_through(self):
        """Once the payload is set aside, the live store is written again."""
        payload = {"version": 1, "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}
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

    @pytest.mark.asyncio
    async def test_removing_the_entry_removes_the_copy_too(self):
        """A deleted config entry leaves neither file behind."""
        with _stores_by_key() as stores:
            await StateManager.async_remove_store(AsyncMock(), "test_entry")

        stores[_LIVE_STORE_KEY].async_remove.assert_awaited_once()
        for key in _SET_ASIDE_KEYS:
            stores[key].async_remove.assert_awaited_once()


_SET_ASIDE_KEYS = (_SET_ASIDE_KEY, f"{_SET_ASIDE_KEY}.1", f"{_SET_ASIDE_KEY}.2")
"""The keys the copies of one entry's unreadable payloads are kept under."""


def _truncated_into(store: AsyncMock):
    """Return a save side effect after which *store* loads a cut-off payload."""

    def _save(_data):
        store.async_load.return_value = {"version": 1}

    return _save


def _poisoned_payload(gain: float) -> dict[str, object]:
    """Return a stored payload that loads with one entry reset."""
    return {"version": 1, "mpc": {"k1": {"gain_est": gain, "kalman_P": None}}}


class TestEveryDistinctPayloadIsKept:
    """Each distinct unreadable payload gets a copy before the store is written.

    A payload read after an earlier copy was taken holds what was learned
    since, and the next save replaces it as much as it replaced the first.
    """

    @staticmethod
    async def _load_and_save(stores, payload: dict[str, object]) -> StateManager:
        mgr = StateManager(_hass_double(), "test_entry")
        stores[_LIVE_STORE_KEY].async_load.return_value = payload
        await mgr.load()
        mgr.mark_dirty()
        await mgr.save()
        return mgr

    @staticmethod
    def _stored(stores, key: str, payload: dict[str, object]) -> None:
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
    _PAYLOAD = {"version": 1, "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}

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
        older = {"version": 1, "mpc": {"k1": {"gain_est": 0.1, "kalman_P": None}}}
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


class TestAFailedCopyThatRecovers:
    """Once the copy can be written again, the session's state is saved.

    A copy that failed at load is retried on a timer, and by a runtime save
    that falls due first, after a minute and then at a doubling interval, so
    a disk that stays full is not written to on every save.
    """

    _LIVE_KEY = "better_thermostat_retry_entry_state"
    _COPY_KEY = "better_thermostat_retry_entry_state.corrupt"
    _PAYLOAD = {"version": 1, "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}

    @contextmanager
    def _disk(self, disk: dict[str, bool | int]):
        """Fail every write of a set-aside copy while ``disk["full"]`` is set."""
        write = storage.Store._async_write_data

        async def _write(store, data):
            if ".corrupt" in store.key and disk["full"]:
                disk["attempts"] = disk.get("attempts", 0) + 1
                raise WriteError("disk full")
            await write(store, data)

        with patch.object(storage.Store, "_async_write_data", _write):
            yield

    async def _loaded(self, hass, hass_storage) -> StateManager:
        hass_storage[self._LIVE_KEY] = {
            "version": 1,
            "minor_version": 1,
            "key": self._LIVE_KEY,
            "data": self._PAYLOAD,
        }
        manager = StateManager(hass, "retry_entry")
        await manager.load()
        return manager

    @staticmethod
    async def _runtime_save(hass, manager: StateManager) -> None:
        """Schedule a runtime save and let it run."""
        manager.mark_dirty()
        manager.schedule_delay_save(delay_seconds=1.0)
        await hass.async_block_till_done(wait_background_tasks=True)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
        await hass.async_block_till_done(wait_background_tasks=True)

    async def test_a_runtime_save_after_recovery_lands(self, hass, hass_storage):
        """The first runtime save once the retry is due writes copy and state."""
        clock = {"now": 1000.0}
        disk: dict[str, bool | int] = {"full": True}
        with self._disk(disk), patch(f"{_SM}.monotonic", lambda: clock["now"]):
            manager = await self._loaded(hass, hass_storage)
            disk["full"] = False
            await self._runtime_save(hass, manager)
            assert self._COPY_KEY not in hass_storage
            assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD

            clock["now"] += 60
            await self._runtime_save(hass, manager)

        assert hass_storage[self._COPY_KEY]["data"] == self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] != self._PAYLOAD
        assert manager.dirty is False

    async def test_the_retry_interval_doubles_while_the_copy_fails(
        self, hass, hass_storage, caplog
    ):
        """A disk that stays full is tried ever less often and reported once."""
        caplog.set_level(logging.DEBUG, logger=_SM)
        clock = {"now": 1000.0}
        disk: dict[str, bool | int] = {"full": True}
        attempts: list[float] = []
        with self._disk(disk), patch(f"{_SM}.monotonic", lambda: clock["now"]):
            manager = await self._loaded(hass, hass_storage)
            start = clock["now"]
            for _ in range(450):
                clock["now"] += 1
                done = disk["attempts"]
                manager.mark_dirty()
                manager.schedule_delay_save(delay_seconds=1.0)
                await hass.async_block_till_done(wait_background_tasks=True)
                if disk["attempts"] > done:
                    attempts.append(clock["now"] - start)

        assert attempts == [60, 180, 420]
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD
        copy_warnings = [
            r
            for r in caplog.records
            if r.name == _SM
            and r.levelno == logging.WARNING
            and "did not reach" in r.getMessage()
        ]
        assert len(copy_warnings) == 1
        assert copy_warnings[0].exc_info is None

    async def test_the_interval_stops_growing_at_an_hour(self, hass, hass_storage):
        """A disk that stays full is retried once an hour at most, not less."""
        clock = {"now": 1000.0}
        disk: dict[str, bool | int] = {"full": True}
        with self._disk(disk), patch(f"{_SM}.monotonic", lambda: clock["now"]):
            manager = await self._loaded(hass, hass_storage)
            for _ in range(20):
                clock["now"] += 3600
                manager.mark_dirty()
                manager.schedule_delay_save(delay_seconds=1.0)
                await hass.async_block_till_done(wait_background_tasks=True)

        assert disk["attempts"] == 21

    async def test_no_copy_is_attempted_while_home_assistant_stops(
        self, hass, hass_storage
    ):
        """While stopping, a copy would only be queued, so it is left for later."""
        disk: dict[str, bool | int] = {"full": True}
        with self._disk(disk):
            manager = await self._loaded(hass, hass_storage)
            disk["full"] = False
            manager.mark_dirty()
            hass.set_state(CoreState.stopping)
            try:
                await manager.flush()
                hass.set_state(CoreState.final_write)
                hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
                await hass.async_block_till_done()
            finally:
                hass.set_state(CoreState.running)

        assert self._COPY_KEY not in hass_storage
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD
        assert manager.dirty is True

    @staticmethod
    async def _advance(hass, freezer, seconds: float) -> None:
        """Move the clock on by *seconds* and let what fell due run."""
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done(wait_background_tasks=True)

    async def test_the_copy_and_the_held_save_land_on_their_own_timer(
        self, hass, hass_storage, freezer
    ):
        """Learning skipped for the copy is saved once the retry is due.

        No further change of state is needed: until something else triggers
        a save, an abrupt stop would otherwise lose that learning.
        """
        disk: dict[str, bool | int] = {"full": True}
        recorded: list[bool] = []
        with self._disk(disk):
            manager = await self._loaded(hass, hass_storage)
            manager.mark_dirty()
            manager.schedule_delay_save(
                pre_save=lambda: recorded.append(True), delay_seconds=1.0
            )
            await hass.async_block_till_done(wait_background_tasks=True)
            disk["full"] = False

            await self._advance(hass, freezer, 30)
            assert self._COPY_KEY not in hass_storage

            await self._advance(hass, freezer, 31)
            await self._advance(hass, freezer, 2)

        assert hass_storage[self._COPY_KEY]["data"] == self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] != self._PAYLOAD
        assert recorded == [True]
        assert manager.dirty is False

    async def test_the_timer_backs_off_while_the_copy_fails(
        self, hass, hass_storage, freezer
    ):
        """Each failed timed try waits twice as long as the one before."""
        disk: dict[str, bool | int] = {"full": True}
        attempts: list[int] = []
        with self._disk(disk):
            manager = await self._loaded(hass, hass_storage)
            manager.mark_dirty()
            for second in range(1, 450):
                done = disk["attempts"]
                await self._advance(hass, freezer, 1)
                if disk["attempts"] > done:
                    attempts.append(second)
            manager.close()

        assert attempts == [60, 180, 420]
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD

    async def test_a_closed_manager_leaves_no_timer_behind(
        self, hass, hass_storage, freezer
    ):
        """A removed entity's manager tries the copy when flushed, not on a timer.

        A timer that outlived the entity would write into a store the next
        entity for the same entry owns by then.
        """
        disk: dict[str, bool | int] = {"full": True}
        with self._disk(disk):
            manager = await self._loaded(hass, hass_storage)
            manager.mark_dirty()
            manager.close()
            await self._advance(hass, freezer, 2 * 3600)
            tried_before_the_flush = disk["attempts"]

            await manager.flush()
            await self._advance(hass, freezer, 2 * 3600)

        assert tried_before_the_flush == 1
        assert disk["attempts"] == 2
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD

    async def test_a_copy_that_lands_after_close_schedules_no_save(
        self, hass, hass_storage, freezer
    ):
        """A timed copy still under way when the entity goes schedules nothing.

        ``flush()`` makes the final write; a save queued by the copy after
        ``close()`` would write into a store the next entity owns by then.
        """
        disk: dict[str, bool | int] = {"full": True}
        entered = asyncio.Event()
        release = asyncio.Event()
        write = storage.Store._async_write_data

        async def _gated(store, data):
            if ".corrupt" in store.key:
                entered.set()
                await release.wait()
            await write(store, data)

        with self._disk(disk):
            manager = await self._loaded(hass, hass_storage)
            manager.mark_dirty()
            disk["full"] = False
        with patch.object(storage.Store, "_async_write_data", _gated):
            freezer.tick(timedelta(seconds=61))
            async_fire_time_changed(hass, dt_util.utcnow())
            await asyncio.wait_for(entered.wait(), timeout=5)

            manager.close()
            release.set()
            await hass.async_block_till_done(wait_background_tasks=True)
            await self._advance(hass, freezer, 2 * 3600)

        assert hass_storage[self._COPY_KEY]["data"] == self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] == self._PAYLOAD
        assert manager.dirty is True

    async def test_the_final_flush_waits_for_a_copy_under_way(
        self, hass, hass_storage, freezer
    ):
        """A flush during a timed copy saves the state once that copy lands.

        A second try beside the running one may fail where the first one
        succeeds; the running copy then schedules no save after ``close()``,
        so the flush has to wait for it and write the state itself.
        """
        disk: dict[str, bool | int] = {"full": True}
        entered = asyncio.Event()
        release = asyncio.Event()
        write = storage.Store._async_write_data
        gated_writes = {"count": 0}

        async def _gated(store, data):
            if ".corrupt" in store.key:
                gated_writes["count"] += 1
                if gated_writes["count"] > 1:
                    raise WriteError("disk busy")
                entered.set()
                await release.wait()
            await write(store, data)

        with self._disk(disk):
            manager = await self._loaded(hass, hass_storage)
            manager.mark_dirty()
            disk["full"] = False
        with patch.object(storage.Store, "_async_write_data", _gated):
            freezer.tick(timedelta(seconds=61))
            async_fire_time_changed(hass, dt_util.utcnow())
            await asyncio.wait_for(entered.wait(), timeout=5)

            manager.close()
            flush = hass.async_create_task(manager.flush())
            for _ in range(20):
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(flush, timeout=5)
            flushed = hass_storage[self._LIVE_KEY]["data"]

            await hass.async_block_till_done(wait_background_tasks=True)
            await self._advance(hass, freezer, 2 * 3600)

        assert hass_storage[self._COPY_KEY]["data"] == self._PAYLOAD
        assert flushed != self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] == flushed
        assert manager.dirty is False

    async def test_the_final_flush_waits_for_a_runtime_copy_under_way(
        self, hass, hass_storage
    ):
        """A flush during a copy a runtime save started saves the state itself.

        The runtime save tries the due copy in the background and returns. A
        second try beside it may fail where it succeeds, and that copy
        schedules no save after ``close()``, so the flush has to wait for it
        and write the state itself.
        """
        clock = {"now": 1000.0}
        disk: dict[str, bool | int] = {"full": True}
        entered = asyncio.Event()
        release = asyncio.Event()
        write = storage.Store._async_write_data
        gated_writes = {"count": 0}

        async def _gated(store, data):
            if ".corrupt" in store.key:
                gated_writes["count"] += 1
                if gated_writes["count"] > 1:
                    raise WriteError("disk busy")
                entered.set()
                await release.wait()
            await write(store, data)

        with patch(f"{_SM}.monotonic", lambda: clock["now"]):
            with self._disk(disk):
                manager = await self._loaded(hass, hass_storage)
                disk["full"] = False
            with patch.object(storage.Store, "_async_write_data", _gated):
                clock["now"] += 61
                manager.mark_dirty()
                manager.schedule_delay_save(delay_seconds=1.0)
                await asyncio.wait_for(entered.wait(), timeout=5)

                manager.close()
                flush = hass.async_create_task(manager.flush())
                for _ in range(20):
                    await asyncio.sleep(0)
                release.set()
                await asyncio.wait_for(flush, timeout=5)
                flushed = hass_storage[self._LIVE_KEY]["data"]

                await hass.async_block_till_done(wait_background_tasks=True)

        assert gated_writes["count"] == 1
        assert hass_storage[self._COPY_KEY]["data"] == self._PAYLOAD
        assert flushed != self._PAYLOAD
        assert hass_storage[self._LIVE_KEY]["data"] == flushed
        assert manager.dirty is False


class TestStoredVersion:
    """The payload's ``version`` is read as an integer before anything else.

    Every JSON number keeps the migration decision ``version < 1`` it has
    always had and is held, and saved again, as its integer part. Anything
    that is not a finite number, or whose integer part lies outside the range
    the Store can write, sends the payload down the unreadable path,
    which keeps a copy before defaults take its place.
    """

    _ENTRY = {"k1": {"gain_est": 0.5}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("stored", "expected"), [(1, 1), (True, 1), (1.0, 1), (1.9, 1), (2, 2)]
    )
    async def test_a_numeric_version_loads_as_an_integer(self, stored, expected):
        """A bool or float version loads its entries and saves as an int."""
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": stored,
                "mpc": self._ENTRY,
            }
            await mgr.load()
            await mgr.save()

        assert type(mgr.state.version) is int
        assert mgr.state.version == expected
        assert mgr.state.mpc["k1"].gain_est == 0.5
        assert _SET_ASIDE_KEY not in stores
        saved = stores[_LIVE_STORE_KEY].async_save.await_args[0][0]
        assert type(saved["version"]) is int
        assert saved["version"] == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored", [0, False, 0.5, -3])
    async def test_a_version_below_one_is_migrated(self, stored):
        """A number below 1, a bool included, still runs the v0 migration."""
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = {
                "version": stored,
                "mpc": self._ENTRY,
            }
            with patch(
                f"{_SM}._migrate_v0_to_v1", side_effect=_migrate_v0_to_v1
            ) as migrate:
                await mgr.load()

        migrate.assert_called_once()
        assert type(mgr.state.version) is int
        assert mgr.state.version < 1
        assert mgr.state.mpc["k1"].gain_est == 0.5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored",
        [
            "1",
            None,
            [1],
            {"v": 1},
            float("nan"),
            float("inf"),
            1e20,
            2**64,
            -(2**63) - 1,
        ],
    )
    async def test_a_version_that_is_no_number_is_unreadable(self, stored, caplog):
        """A version the Store could not write back keeps a copy and starts fresh."""
        payload = {"version": stored, "mpc": self._ENTRY}
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            with caplog.at_level(logging.WARNING, logger=_SM):
                await mgr.load()

        assert mgr.state.mpc == {}
        assert mgr.state.version == CURRENT_VERSION
        assert "persisted state is unreadable" in caplog.text
        copy = stores[_SET_ASIDE_KEY].async_save.await_args[0][0]
        assert copy is payload

    def test_a_payload_without_a_version_deserializes_as_current(self):
        """``_deserialize`` gives a payload without a version the current one."""
        assert _deserialize({"mpc": {}}).version == CURRENT_VERSION


class TestMigrationLeavesItsInputUnchanged:
    """The v0 migration builds a new payload instead of filling in the loaded one."""

    def test_the_input_keeps_its_keys(self):
        """The loaded mapping is the same before and after the migration."""
        raw = {"mpc": {"k": {"gain_est": 0.5}}, "presets": {"eco": 18.0}}
        before = {"mpc": {"k": {"gain_est": 0.5}}, "presets": {"eco": 18.0}}

        result = _migrate_v0_to_v1(raw)

        assert raw == before
        assert result is not raw
        assert result == {
            **before,
            "version": 1,
            "pid": {},
            "tpi": {},
            "thermal": {},
            "filters": {},
        }

    def test_each_default_section_is_its_own_mapping(self):
        """No two migrated payloads, or sections, share a default mapping."""
        first = _migrate_v0_to_v1({})
        second = _migrate_v0_to_v1({})

        assert first["mpc"] is not second["mpc"]
        assert first["pid"] is not first["tpi"]

    @pytest.mark.asyncio
    async def test_a_v0_payload_is_set_aside_as_migrated(self):
        """A v0 payload with a poisoned entry is kept as the migration left it.

        The live payload stays as Home Assistant loaded it, and the copy set
        aside carries the v1 defaults the migration added.
        """
        payload = {"mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}
        with _stores_by_key() as stores:
            mgr = StateManager(_hass_double(), "test_entry")
            stores[_LIVE_STORE_KEY].async_load.return_value = payload
            await mgr.load()

        assert payload == {"mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}}}
        copy = stores[_SET_ASIDE_KEY].async_save.await_args[0][0]
        assert copy == {
            "version": 1,
            "mpc": {"k1": {"gain_est": 0.5, "kalman_P": None}},
            "pid": {},
            "tpi": {},
            "thermal": {},
            "filters": {},
        }
