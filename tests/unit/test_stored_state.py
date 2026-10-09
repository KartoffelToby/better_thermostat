"""Pin the on-disk shape of the unified runtime state store.

The golden fixture is the exact payload Home Assistant's Store writes for a
fully populated ``RuntimeState``: every stored key name, the key order, and
the JSON shape of every value. Releases that read this store (including the
1.9 line after a downgrade) depend on it, so any difference is a format
change, not a refactor.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import fields
import json
import math
from pathlib import Path

from homeassistant.helpers.json import prepare_save_json
import pytest

from custom_components.better_thermostat.utils import state_manager
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcState,
    TrvProfile,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    ControllerSnapshot,
)
from custom_components.better_thermostat.utils.calibration.pid import PIDState
from custom_components.better_thermostat.utils.calibration.tpi import TpiState
from custom_components.better_thermostat.utils.state_manager import (
    _STORED_MPC_KEYS,
    _STORED_MPC_V2_REID_KEYS,
    _STORED_PID_KEYS,
    _STORED_ROOM_TEMPERATURE_EMA,
    _STORED_TEMPERATURE_SLOPE,
    FilterState,
    MpcV2ReidData,
    MpcV2StateData,
    RuntimeState,
    ThermalStats,
    _deserialize,
    _serialize,
    write_filters,
    write_mpc_state,
    write_mpc_v2_reid,
    write_mpc_v2_state,
    write_pid_state,
    write_thermal,
    write_tpi_state,
)
from custom_components.better_thermostat.utils.stored_state import (
    StoredFilterState,
    StoredMpcState,
    StoredMpcV2Reid,
    StoredMpcV2State,
    StoredPidState,
    StoredRuntimeState,
    StoredThermalStats,
    StoredTpiState,
)

GOLDEN_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "stored_runtime_state.json"
)


def _populated_mpc() -> MpcState:
    """Return an MPC state with every field away from its default."""
    return MpcState(
        last_percent=41.5,
        last_update_ts=1700000001.0,
        last_target_temperature=21.5,
        ema_slope=0.012,
        gain_est=0.031,
        loss_est=0.007,
        ka_est=0.0025,
        solar_gain_est=0.4,
        last_cycle_temperature=20.75,
        last_time=1700000002.0,
        last_trv_temperature=24.5,
        last_trv_temperature_ts=1700000003.0,
        last_window_open_ts=1700000004.0,
        dead_zone_hits=3,
        min_effective_percent=12.0,
        last_learn_time=1700000005.0,
        last_learn_temperature=20.5,
        last_residual_time=1700000006.0,
        virtual_temperature=20.9,
        virtual_temperature_ts=1700000007.0,
        last_sensor_temperature=22.25,
        last_room_temperature=20.8,
        last_room_temperature_ts=1700000008.0,
        perf_curve={"heat": {"10": 0.02, "50": 0.11}, "idle": {"0": -0.01}},
        trv_profile=TrvProfile.LINEAR,
        profile_confidence=0.66,
        profile_samples=17,
        u_integral=123.5,
        time_integral=3600.0,
        last_integration_ts=1700000009.0,
        created_ts=1699990000.0,
        loss_learn_count=5,
        gain_learn_count=7,
        is_calibration_active=True,
        recent_errors=deque([0.1, -0.2, 0.05], maxlen=20),
        regime_boost_active=True,
        consecutive_insufficient_heat=2,
        kalman_P=0.37,
        tolerance_hold_active=True,
    )


def _populated_snapshot() -> dict[str, object]:
    """Return an MPC v2 controller snapshot as the store holds it."""
    return ControllerSnapshot(
        v=2,
        x_hat=[20.5, 35.25],
        kalman_P=[[0.5, 0.01], [0.01, 0.75]],
        D_hat_K_per_min=-0.002,
        last_u=0.42,
        e_integral_K_min=1.5,
        u_history=[0.4, 0.41, 0.42],
        rg_v=21.0,
        last_t_s=86400.0,
        next_mpc_t_s=86700.0,
        last_mpc_t_s=86100.0,
        planning_disturbance=0.003,
    ).to_mapping()


def _populated_state() -> RuntimeState:
    """Return a runtime state with every section and field populated.

    Each keyed section also holds an entry at its defaults, which pins how
    the store writes the unset (null) values.
    """
    return RuntimeState(
        version=1,
        mpc={"bt:full": _populated_mpc(), "bt:defaults": MpcState()},
        mpc_v2={
            "bt:full": MpcV2StateData(
                last_percent=37.5,
                last_compute_ts=1700000010.0,
                created_ts=1699990001.0,
                outdoor_fallback_logged=True,
                snapshot=_populated_snapshot(),
            ),
            "bt:defaults": MpcV2StateData(),
        },
        mpc_v2_reid={
            "bt:full": MpcV2ReidData(
                tau_room_minutes=480.0,
                gain_heater=0.055,
                fitted_ts=1700000011.0,
                rmse_prior_kelvin=0.31,
                rmse_fit_kelvin=0.12,
                n_segments=9,
            ),
            "bt:defaults": MpcV2ReidData(),
        },
        pid={
            "bt:full": PIDState(
                pid_integral=4.5,
                pid_last_meas=20.6,
                pid_last_error=0.9,
                pid_last_time=1700000012.0,
                pid_kp=55.0,
                pid_ki=0.012,
                pid_kd=1800.0,
                auto_tune=True,
                last_tune_ts=1700000013.0,
                last_delta_sign=-1,
                last_error_sign=1,
                previous_abs_error=0.8,
                last_abs_error=0.6,
                ema_slope=-0.004,
                last_percent=33.0,
                last_output_change_ts=1700000014.0,
                last_target_temperature=21.5,
            ),
            "bt:defaults": PIDState(),
        },
        tpi={
            "bt:full": TpiState(last_percent=60.0, last_update_ts=1700000015.0),
            "bt:defaults": TpiState(),
        },
        thermal=ThermalStats(heating_power=0.0123, heat_loss_rate=0.0045),
        filters=FilterState(room_temperature_ema=20.7, temperature_slope=0.0021),
    )


def _stored_bytes(state: RuntimeState) -> bytes:
    """Return the bytes Home Assistant's Store writes for *state*'s payload."""
    mode, json_data = prepare_save_json(dict(_serialize(state)))
    assert mode == "wb"
    assert isinstance(json_data, bytes)
    return json_data


def test_a_populated_state_is_written_as_the_golden_payload():
    """Every stored key, its order and its value shape match the fixture."""
    assert _stored_bytes(_populated_state()) == GOLDEN_PATH.read_bytes().rstrip(b"\n")


def test_the_golden_payload_reads_back_as_the_populated_state(
    monkeypatch: pytest.MonkeyPatch,
):
    """Every stored key is read into its field, so nothing is left at its default.

    The fixture's re-identification results sit outside the plausible band
    the reader enforces, so the band is opened for this read.
    """
    unbounded = (-math.inf, math.inf)
    monkeypatch.setattr(f"{state_manager.__name__}.TAU_ROOM_BOUNDS_MIN", unbounded)
    monkeypatch.setattr(f"{state_manager.__name__}.GAIN_HEATER_BOUNDS", unbounded)
    poisoned: list[str] = []
    restored = _deserialize(json.loads(GOLDEN_PATH.read_bytes()), poisoned=poisoned)
    assert poisoned == []
    assert restored == _populated_state()


def test_serializing_leaves_the_live_state_unshared():
    """The written payload holds copies, not the live containers."""
    state = _populated_state()
    data = _serialize(state)
    state.mpc["bt:full"].recent_errors.append(9.0)
    state.mpc["bt:full"].perf_curve["heat"]["90"] = 0.5
    snapshot = state.mpc_v2["bt:full"].snapshot
    assert isinstance(snapshot, dict)
    x_hat = snapshot["x_hat"]
    assert isinstance(x_hat, list)
    x_hat.append(1.0)
    assert _stored_bytes(_populated_state()) == prepare_save_json(dict(data))[1]


_FILTER_KEYS = {
    "room_temperature_ema": _STORED_ROOM_TEMPERATURE_EMA,
    "temperature_slope": _STORED_TEMPERATURE_SLOPE,
}

# Each persisted dataclass, its on-disk TypedDict, and the fields stored
# under a name other than their own.
_SECTIONS: list[tuple[type, type, Mapping[str, str]]] = [
    (MpcState, StoredMpcState, _STORED_MPC_KEYS),
    (MpcV2StateData, StoredMpcV2State, {}),
    (MpcV2ReidData, StoredMpcV2Reid, _STORED_MPC_V2_REID_KEYS),
    (PIDState, StoredPidState, _STORED_PID_KEYS),
    (TpiState, StoredTpiState, {}),
    (ThermalStats, StoredThermalStats, {}),
    (FilterState, StoredFilterState, _FILTER_KEYS),
    (RuntimeState, StoredRuntimeState, {}),
]


@pytest.mark.parametrize(
    ("persisted", "stored", "renamed"),
    _SECTIONS,
    ids=[persisted.__name__ for persisted, _, _ in _SECTIONS],
)
def test_every_dataclass_field_has_exactly_one_stored_key(
    persisted: type, stored: type, renamed: Mapping[str, str]
):
    """A field added to a persisted dataclass cannot go unwritten.

    The stored keys are the field names, with the legacy store names
    substituted, and every key is required.
    """
    expected = {renamed.get(f.name, f.name) for f in fields(persisted)}
    assert set(renamed) <= {f.name for f in fields(persisted)}
    assert stored.__required_keys__ == expected
    assert stored.__optional_keys__ == frozenset()


_WRITERS: list[tuple[Callable[[], Mapping[str, object]], type]] = [
    (lambda: write_mpc_state(MpcState()), StoredMpcState),
    (lambda: write_mpc_v2_state(MpcV2StateData()), StoredMpcV2State),
    (lambda: write_mpc_v2_reid(MpcV2ReidData()), StoredMpcV2Reid),
    (lambda: write_pid_state(PIDState()), StoredPidState),
    (lambda: write_tpi_state(TpiState()), StoredTpiState),
    (lambda: write_thermal(ThermalStats()), StoredThermalStats),
    (lambda: write_filters(FilterState()), StoredFilterState),
    (lambda: _serialize(RuntimeState()), StoredRuntimeState),
]


@pytest.mark.parametrize(
    ("write", "stored"), _WRITERS, ids=[stored.__name__ for _, stored in _WRITERS]
)
def test_each_writer_emits_exactly_its_stored_keys(
    write: Callable[[], Mapping[str, object]], stored: type
):
    """A writer produces every key of its on-disk shape and nothing else."""
    assert set(write()) == stored.__required_keys__
