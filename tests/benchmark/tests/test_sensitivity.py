"""Tests for plant-profile override and stabilisation phase in the runner."""

from __future__ import annotations

from tests.benchmark.adapters.baselines import IdealOracleAdapter
from tests.benchmark.plant import (
    PROFILE_FAST_SMALL,
    PROFILE_LARGE_SLOW,
    PROFILE_STANDARD,
)
from tests.benchmark.runner import PLANT_PROFILES, run_scenario
from tests.benchmark.scenarios import S01_SETPOINT_STEP_SMALL


def test_plant_profiles_registry_complete():
    """The CLI plant registry exposes every built-in profile."""
    assert "fast_small" in PLANT_PROFILES
    assert "standard" in PLANT_PROFILES
    assert "large_slow" in PLANT_PROFILES
    assert "underfloor" in PLANT_PROFILES


def test_plant_override_changes_dynamics():
    """Two plant overrides yield measurably different valve usage.

    Uses ``integral_valve_pct_min`` as a proxy.
    """
    standard = run_scenario(
        IdealOracleAdapter(),
        S01_SETPOINT_STEP_SMALL,
        plant_params=PROFILE_STANDARD,
        stabilisation_min=30.0,
    )
    large_slow = run_scenario(
        IdealOracleAdapter(),
        S01_SETPOINT_STEP_SMALL,
        plant_params=PROFILE_LARGE_SLOW,
        stabilisation_min=30.0,
    )
    # Large-slow has bigger heat losses → higher valve usage to maintain SP.
    assert (
        large_slow.metrics.integral_valve_pct_min
        > standard.metrics.integral_valve_pct_min
    ), "Large-slow plant should require more valve time than standard"


def test_stabilisation_reduces_initial_undershoot():
    """Stabilisation > 0 reduces the post-step undershoot vs cold-start.

    Cold-start uses ``stabilisation_min == 0``.
    """
    cold = run_scenario(
        IdealOracleAdapter(), S01_SETPOINT_STEP_SMALL, stabilisation_min=0.0
    )
    warm = run_scenario(
        IdealOracleAdapter(), S01_SETPOINT_STEP_SMALL, stabilisation_min=60.0
    )
    assert warm.metrics.max_undershoot_K < cold.metrics.max_undershoot_K + 1e-9
    # And ideally strictly less; allow ties due to discretisation.


def test_run_scenario_with_explicit_plant_override():
    """Passing an explicit ``plant_params`` runs without error and produces metrics."""
    result = run_scenario(
        IdealOracleAdapter(),
        S01_SETPOINT_STEP_SMALL,
        plant_params=PROFILE_FAST_SMALL,
        stabilisation_min=30.0,
    )
    assert result.scenario == S01_SETPOINT_STEP_SMALL.name
    # IdealOracle on a well-matched plant should not exhibit overshoot in S01.
    assert result.metrics.max_overshoot_K < 0.5
