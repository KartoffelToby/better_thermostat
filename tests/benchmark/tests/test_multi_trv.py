"""Multi-TRV plant and runner tests."""

from __future__ import annotations

from dataclasses import replace
import math

import pytest

from tests.benchmark import multi_trv_runner
from tests.benchmark.adapters.baselines import IdealOracleAdapter
from tests.benchmark.adapters.pid_adapter import PidAdapter
from tests.benchmark.multi_trv_plant import (
    PROFILE_MULTI_ASYMMETRIC,
    PROFILE_MULTI_HETEROGENEOUS,
    PROFILE_MULTI_SYMMETRIC,
    MultiTrvPlant,
    MultiTrvPlantParams,
    MultiTrvPlantState,
)
from tests.benchmark.multi_trv_runner import (
    _equivalent_single_plant,
    make_multi_trv_adapter,
    run_multi_trv_scenario,
)
from tests.benchmark.scenarios import S01_SETPOINT_STEP_SMALL


def _initial(n: int = 3, T0: float = 20.0) -> MultiTrvPlantState:
    return MultiTrvPlantState(T_room_C=T0, T_rads_C=[T0] * n)


def test_plant_init_validates_state_length():
    """Plant init validates state length."""
    with pytest.raises(ValueError, match="T_rads_C"):
        MultiTrvPlant(
            params=PROFILE_MULTI_SYMMETRIC,
            initial=MultiTrvPlantState(T_room_C=20.0, T_rads_C=[20.0, 20.0]),
        )


def test_plant_init_validates_gains_length():
    """Plant init validates gains length."""
    bad = MultiTrvPlantParams(n_trvs=3, gain_heaters=[2.0, 2.0])
    with pytest.raises(ValueError, match="gain_heaters"):
        MultiTrvPlant(params=bad, initial=_initial())


def test_plant_init_validates_coupling_length():
    """Plant init validates coupling length."""
    bad = MultiTrvPlantParams(n_trvs=3, coupling_rad_room=[1.0])
    with pytest.raises(ValueError, match="coupling_rad_room"):
        MultiTrvPlant(params=bad, initial=_initial())


def test_plant_init_validates_offset_length():
    """Plant init validates offset length."""
    bad = MultiTrvPlantParams(n_trvs=3, trv_sensor_offsets_K=[0.0])
    with pytest.raises(ValueError, match="trv_sensor_offsets_K"):
        MultiTrvPlant(params=bad, initial=_initial())


def test_step_rejects_wrong_u_length():
    """Step rejects wrong u length."""
    plant = MultiTrvPlant(PROFILE_MULTI_SYMMETRIC, _initial())
    with pytest.raises(ValueError, match="u_per_trv"):
        plant.step(dt_s=30.0, u_per_trv=[0.5, 0.5], T_outdoor_C=5.0)


def test_step_zero_dt_is_noop():
    """Step zero dt is noop."""
    plant = MultiTrvPlant(PROFILE_MULTI_SYMMETRIC, _initial())
    before = list(plant.state.T_rads_C)
    plant.step(dt_s=0.0, u_per_trv=[1.0, 1.0, 1.0], T_outdoor_C=5.0)
    assert plant.state.T_rads_C == before


def test_full_valve_warms_room_and_radiators():
    """Full valve warms room and radiators."""
    plant = MultiTrvPlant(PROFILE_MULTI_SYMMETRIC, _initial(T0=20.0))
    for _ in range(120):  # 1 hour at 30 s ticks
        plant.step(dt_s=30.0, u_per_trv=[1.0, 1.0, 1.0], T_outdoor_C=5.0)
    assert plant.state.T_room_C > 20.0
    for T_rad in plant.state.T_rads_C:
        assert T_rad > 20.0


def test_closed_valves_drift_toward_outdoor():
    """Closed valves drift toward outdoor."""
    plant = MultiTrvPlant(PROFILE_MULTI_SYMMETRIC, _initial(T0=22.0))
    for _ in range(1200):  # 10 h
        plant.step(dt_s=30.0, u_per_trv=[0.0, 0.0, 0.0], T_outdoor_C=0.0)
    assert plant.state.T_room_C < 22.0


def test_heterogeneous_profile_zeroes_deadband_trv():
    """Heterogeneous profile zeroes deadband trv."""
    plant = MultiTrvPlant(PROFILE_MULTI_HETEROGENEOUS, _initial(T0=20.0))
    # Middle TRV has a 22 % deadband. A 10 % command goes through 2.0 % on
    # the others but is zeroed on TRV[1].
    # Run long enough that the asymmetry shows up in the radiator
    # temperatures.
    for _ in range(60):
        plant.step(dt_s=30.0, u_per_trv=[0.10, 0.10, 0.10], T_outdoor_C=10.0)
    assert plant.state.T_rads_C[0] > plant.state.T_rads_C[1]
    assert plant.state.T_rads_C[2] > plant.state.T_rads_C[1]


def test_reported_temps_apply_sensor_offset():
    """Reported temps apply sensor offset."""
    plant = MultiTrvPlant(PROFILE_MULTI_ASYMMETRIC, _initial(T0=20.0))
    reported = plant.reported_trv_temps()
    actual = plant.state.T_rads_C
    # Asymmetric profile: offsets [-1.5, 0.0, 0.5]
    assert math.isclose(reported[0], actual[0] - 1.5)
    assert math.isclose(reported[1], actual[1])
    assert math.isclose(reported[2], actual[2] + 0.5)


def test_valve_clamping_below_zero_and_above_one():
    """Valve clamping below zero and above one."""
    plant = MultiTrvPlant(PROFILE_MULTI_SYMMETRIC, _initial())
    # Out-of-range u values are clamped to [0, 1] internally.
    plant.step(dt_s=30.0, u_per_trv=[-0.5, 2.0, 0.5], T_outdoor_C=5.0)
    # No exception, state is sane.
    assert all(math.isfinite(T) for T in plant.state.T_rads_C)


def test_equivalent_single_plant_sums_gains_and_coupling():
    """Equivalent single plant sums gains and coupling."""
    eq = _equivalent_single_plant(PROFILE_MULTI_ASYMMETRIC)
    assert eq.tau_room_min == PROFILE_MULTI_ASYMMETRIC.tau_room_min
    assert eq.tau_rad_min == PROFILE_MULTI_ASYMMETRIC.tau_rad_min
    assert math.isclose(eq.gain_heater, sum(PROFILE_MULTI_ASYMMETRIC.gain_heaters))
    assert math.isclose(
        eq.coupling_rad_room, sum(PROFILE_MULTI_ASYMMETRIC.coupling_rad_room)
    )


def test_make_multi_trv_adapter_threads_plant_for_oracle():
    """Make multi trv adapter threads plant for oracle."""
    adapter = make_multi_trv_adapter("ideal_oracle", PROFILE_MULTI_SYMMETRIC)
    # The oracle now sees the aggregated equivalent plant — sum of gains, etc.
    assert isinstance(adapter, IdealOracleAdapter)
    assert math.isclose(
        adapter._plant.gain_heater, sum(PROFILE_MULTI_SYMMETRIC.gain_heaters)
    )


def test_make_multi_trv_adapter_for_non_plant_aware():
    """Make multi trv adapter for non plant aware."""
    adapter = make_multi_trv_adapter("pid", PROFILE_MULTI_SYMMETRIC)
    assert isinstance(adapter, PidAdapter)


def test_run_multi_trv_scenario_completes_and_marks_label():
    """Run multi trv scenario completes and marks label."""
    adapter = make_multi_trv_adapter("pid", PROFILE_MULTI_SYMMETRIC)
    result = run_multi_trv_scenario(
        adapter,
        S01_SETPOINT_STEP_SMALL,
        PROFILE_MULTI_SYMMETRIC,
        _initial(),
        step_s=30.0,
        stabilisation_min=0.0,  # skip pre-warm for test speed
    )
    assert result.scenario.endswith("[multi-TRV]")
    assert result.controller == "pid"
    m = result.metrics
    assert m.max_overshoot_K >= 0.0
    assert m.rmse_tracking_K >= 0.0


def test_run_multi_trv_scenario_with_stabilisation():
    """Run multi trv scenario with stabilisation."""
    adapter = make_multi_trv_adapter("ideal_oracle", PROFILE_MULTI_SYMMETRIC)
    # Exercise the stabilisation path.
    result = run_multi_trv_scenario(
        adapter,
        S01_SETPOINT_STEP_SMALL,
        PROFILE_MULTI_SYMMETRIC,
        _initial(),
        step_s=60.0,
        stabilisation_min=5.0,
    )
    assert result.controller == "ideal_oracle"
    assert math.isfinite(result.metrics.rmse_tracking_K)


def test_heterogeneous_multi_trv_run_keeps_room_bounded():
    """Heterogeneous multi trv run keeps room bounded."""
    adapter = make_multi_trv_adapter("pid", PROFILE_MULTI_HETEROGENEOUS)
    result = run_multi_trv_scenario(
        adapter,
        S01_SETPOINT_STEP_SMALL,
        PROFILE_MULTI_HETEROGENEOUS,
        _initial(),
        step_s=60.0,
        stabilisation_min=0.0,
    )
    # Even with a deadbanded TRV the run completes and produces finite metrics.
    assert math.isfinite(result.metrics.rmse_tracking_K)


def test_deadband_length_mismatch_raises():
    """A deadband list that is neither empty nor n_trvs long is rejected."""
    params = replace(PROFILE_MULTI_SYMMETRIC, deadband_pcts_per_trv=[10.0])
    with pytest.raises(ValueError):
        MultiTrvPlant(
            params, MultiTrvPlantState(T_room_C=20.0, T_rads_C=[20.0, 20.0, 20.0])
        )


def test_run_multi_trv_scenario_rejects_non_positive_step_s():
    """run_multi_trv_scenario rejects non positive step_s."""
    with pytest.raises(ValueError):
        run_multi_trv_scenario(
            PidAdapter(),
            S01_SETPOINT_STEP_SMALL,
            PROFILE_MULTI_SYMMETRIC,
            MultiTrvPlantState(T_room_C=20.0, T_rads_C=[20.0, 20.0, 20.0]),
            step_s=0.0,
        )


def test_run_multi_trv_scenario_honors_scenario_stabilisation_override(monkeypatch):
    """A per-scenario stabilisation override beats the caller default."""
    seen: dict[str, float] = {}

    def _spy(plant, scenario, stabilisation_min, step_s):
        seen["min"] = stabilisation_min

    monkeypatch.setattr(multi_trv_runner, "_stabilise_multi_trv", _spy)
    scenario = replace(S01_SETPOINT_STEP_SMALL, stabilisation_min=7.0, duration_min=1)
    run_multi_trv_scenario(
        PidAdapter(),
        scenario,
        PROFILE_MULTI_SYMMETRIC,
        MultiTrvPlantState(T_room_C=20.0, T_rads_C=[20.0, 20.0, 20.0]),
        stabilisation_min=60.0,
    )
    assert seen["min"] == 7.0
