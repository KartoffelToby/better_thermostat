"""End-to-end smoke test: MPC adapter against S01."""

from __future__ import annotations

import math

from tests.benchmark.adapters.mpc_adapter import MpcAdapter
from tests.benchmark.runner import run_scenario
from tests.benchmark.scenarios import S01_SETPOINT_STEP_SMALL


def test_mpc_against_s01_runs_to_completion():
    """End-to-end: MPC adapter runs through S01 without crashes."""
    adapter = MpcAdapter()
    result = run_scenario(adapter, S01_SETPOINT_STEP_SMALL)
    # Smoke-level expectations only — assert nothing crashed and produced
    # something sensible.
    assert result.controller == "mpc"
    assert result.scenario == S01_SETPOINT_STEP_SMALL.name
    # Metric values are finite numbers (settling may legitimately be inf if
    # the algorithm fails to converge — we don't assert PASS here).
    m = result.metrics
    # Finite (not NaN/inf) is the real invariant; >= 0 alone would admit inf.
    assert math.isfinite(m.max_overshoot_K)
    assert math.isfinite(m.max_undershoot_K)
    assert (m.settling_time_min >= 0.0) or math.isinf(m.settling_time_min)
    assert math.isfinite(m.rmse_tracking_K)
    assert math.isfinite(m.valve_cycle_count)
    assert math.isfinite(m.integral_valve_pct_min)


def test_mpc_run_is_deterministic():
    """Two independent MPC runs of S01 produce byte-identical metrics.

    The production MPC uses ``random.random()`` for its hybrid-learning
    forced calibration; the adapter seeds a deterministic stand-in so the
    benchmark's reproducibility guarantee holds for MPC too. This guards
    against that seeding regressing.
    """
    a = run_scenario(MpcAdapter(), S01_SETPOINT_STEP_SMALL).metrics
    b = run_scenario(MpcAdapter(), S01_SETPOINT_STEP_SMALL).metrics
    assert a == b
