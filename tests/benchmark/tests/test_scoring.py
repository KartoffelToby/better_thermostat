"""Tests for the weighted scoring system (Phase F)."""

from __future__ import annotations

from dataclasses import replace
import math

import pytest

from tests.benchmark.metrics import MetricValues
from tests.benchmark.scoring import (
    PROFILES,
    UserProfile,
    actuator_score,
    comfort_score,
    compute_scores,
    energy_score,
)


def _zero_metrics(**overrides) -> MetricValues:
    base = MetricValues(
        max_overshoot_K=0.0,
        max_undershoot_K=0.0,
        settling_time_min=10.0,
        steady_state_error_K=0.0,
        rmse_tracking_K=0.0,
        valve_cycle_count=0,
        integral_valve_pct_min=1000.0,
        total_valve_travel_pct=100.0,
        time_above_setpoint_K_h=0.0,
        time_below_setpoint_K_h=0.0,
        valve_sweet_spot_residency_pct=0.0,
    )
    return replace(base, **overrides)


# ---------- Profile invariants ----------


def test_profiles_weights_sum_to_one():
    """Every UserProfile's weights sum to 1.0 (constructor invariant)."""
    for p in PROFILES.values():
        assert math.isclose(p.w_comfort + p.w_actuator + p.w_energy, 1.0, abs_tol=1e-6)


def test_profile_rejects_invalid_weights():
    """Constructor refuses weights that don't sum to 1."""
    with pytest.raises(ValueError):
        UserProfile("bad", 0.5, 0.5, 0.5)


# ---------- Comfort score ----------


def test_comfort_score_matches_oracle_returns_one():
    """A controller matching the oracle exactly scores 1.0 on comfort."""
    oracle = _zero_metrics()
    candidate = _zero_metrics()
    assert comfort_score(candidate, oracle) == pytest.approx(1.0)


def test_comfort_score_overshoot_penalty():
    """Extra overshoot drops the comfort score proportionally."""
    oracle = _zero_metrics()
    candidate = _zero_metrics(max_overshoot_K=1.0)
    # 1 K excess overshoot → comfort drops by 0.4 (weight in comfort)
    assert comfort_score(candidate, oracle) == pytest.approx(0.6, abs=0.01)


def test_comfort_score_inf_settling_penalised():
    """Inf settling drives comfort below 0.6 (most of the 0.4 settling penalty)."""
    oracle = _zero_metrics()
    candidate = _zero_metrics(settling_time_min=math.inf)
    assert comfort_score(candidate, oracle) < 0.7


def test_comfort_score_clamps_to_zero():
    """Catastrophic comfort fails (overshoot + settling + ss_err) clamp at 0."""
    oracle = _zero_metrics()
    candidate = _zero_metrics(
        max_overshoot_K=10.0, settling_time_min=math.inf, steady_state_error_K=5.0
    )
    assert comfort_score(candidate, oracle) == pytest.approx(0.0)


# ---------- Actuator score ----------


def test_actuator_score_matches_oracle_returns_one():
    """Matching the oracle's travel scores 1.0."""
    oracle = _zero_metrics(total_valve_travel_pct=500.0)
    candidate = _zero_metrics(total_valve_travel_pct=500.0)
    assert actuator_score(candidate, oracle) == pytest.approx(1.0)


def test_actuator_score_5x_travel_scores_zero():
    """5× the oracle's travel scores 0."""
    oracle = _zero_metrics(total_valve_travel_pct=500.0)
    candidate = _zero_metrics(total_valve_travel_pct=2500.0)
    assert actuator_score(candidate, oracle) == pytest.approx(0.0)


def test_actuator_score_less_travel_than_oracle_scores_one():
    """Using *less* travel than the oracle still caps at 1.0."""
    oracle = _zero_metrics(total_valve_travel_pct=500.0)
    candidate = _zero_metrics(total_valve_travel_pct=100.0)
    assert actuator_score(candidate, oracle) == pytest.approx(1.0)


def test_actuator_score_handles_low_oracle_travel():
    """When the oracle barely moved, the candidate is compared against a floor."""
    oracle = _zero_metrics(total_valve_travel_pct=0.5)
    candidate = _zero_metrics(total_valve_travel_pct=250.0)
    # Floor of 500 → 250/500 = 0.5 penalty → score 0.5
    assert 0.4 < actuator_score(candidate, oracle) < 0.6


# ---------- Energy score ----------


def test_energy_score_matches_oracle_returns_one():
    """Matching the oracle's integral flow scores 1.0."""
    oracle = _zero_metrics(integral_valve_pct_min=5000.0)
    candidate = _zero_metrics(integral_valve_pct_min=5000.0)
    assert energy_score(candidate, oracle) == pytest.approx(1.0)


def test_energy_score_double_oracle_scores_zero():
    """Using 2× the oracle's integral flow scores 0."""
    oracle = _zero_metrics(integral_valve_pct_min=5000.0)
    candidate = _zero_metrics(integral_valve_pct_min=10000.0)
    assert energy_score(candidate, oracle) == pytest.approx(0.0)


def test_energy_score_low_oracle_neutral_for_matching_candidate():
    """When the oracle barely heated, a candidate near it still scores ~1.0."""
    oracle = _zero_metrics(integral_valve_pct_min=20.0)
    candidate = _zero_metrics(integral_valve_pct_min=25.0)
    assert energy_score(candidate, oracle) == pytest.approx(1.0 - 5.0 / 100.0)


def test_energy_score_low_oracle_penalises_gross_overuse():
    """A grossly over-heating candidate must not escape via the low-oracle path."""
    oracle = _zero_metrics(integral_valve_pct_min=20.0)
    candidate = _zero_metrics(integral_valve_pct_min=2000.0)
    # Excess of ~1980 pct·min against the 100 floor → fully penalised.
    assert energy_score(candidate, oracle) == pytest.approx(0.0)


def test_energy_score_undershoot_penalised_symmetrically():
    """Heating *less* than the oracle is under-heating, not legitimate saving."""
    oracle = _zero_metrics(integral_valve_pct_min=5000.0)
    candidate = _zero_metrics(integral_valve_pct_min=3000.0)
    # ratio = 0.6 → |0.6 - 1| = 0.4 → score 0.6
    assert energy_score(candidate, oracle) == pytest.approx(0.6)


def test_energy_score_zero_integral_scores_zero():
    """Not heating at all when oracle would heat scores 0."""
    oracle = _zero_metrics(integral_valve_pct_min=5000.0)
    candidate = _zero_metrics(integral_valve_pct_min=0.0)
    assert energy_score(candidate, oracle) == pytest.approx(0.0)


# ---------- compute_scores integration ----------


def test_compute_scores_returns_all_dimensions():
    """The aggregate function returns comfort, actuator, energy, overall."""
    oracle = _zero_metrics(total_valve_travel_pct=500.0, integral_valve_pct_min=5000.0)
    candidate = _zero_metrics(
        max_overshoot_K=0.5, total_valve_travel_pct=750.0, integral_valve_pct_min=5500.0
    )
    profile = PROFILES["balanced"]
    s = compute_scores(candidate, oracle, profile)
    assert 0.0 <= s.comfort <= 1.0
    assert 0.0 <= s.actuator <= 1.0
    assert 0.0 <= s.energy <= 1.0
    assert 0.0 <= s.overall <= 1.0


def test_overall_score_respects_profile_weights():
    """Same dimension scores under different profiles give different overalls."""
    oracle = _zero_metrics(total_valve_travel_pct=500.0)
    # Candidate: perfect comfort, terrible actuator
    candidate = _zero_metrics(total_valve_travel_pct=2500.0)
    score_balanced = compute_scores(candidate, oracle, PROFILES["balanced"]).overall
    score_comfort_first = compute_scores(
        candidate, oracle, PROFILES["comfort_first"]
    ).overall
    score_longevity_first = compute_scores(
        candidate, oracle, PROFILES["longevity_first"]
    ).overall
    # comfort_first should rate this candidate higher (good comfort dominates),
    # longevity_first should rate it lower (bad actuator dominates).
    assert score_comfort_first > score_balanced > score_longevity_first


def test_zero_settling_oracle_still_penalises_slow_candidates():
    """An instantly-settled oracle must not mask slow candidate settling."""
    oracle = _zero_metrics(settling_time_min=0.0)
    slow = _zero_metrics(settling_time_min=10.0)
    instant = _zero_metrics(settling_time_min=0.0)
    assert comfort_score(slow, oracle) < comfort_score(instant, oracle)
