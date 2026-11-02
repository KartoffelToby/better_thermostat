"""Tests for the weighted scoring system."""

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
        integral_valve_percent_minutes=1000.0,
        delivered_heat_K=1.0,
        total_valve_travel_percent=100.0,
        time_above_setpoint_K_h=0.0,
        time_below_setpoint_K_h=0.0,
        valve_sweet_spot_residency_percent=0.0,
        discomfort_K_h=0.0,
        transient_duration_h=8.0,
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


def test_profile_rejects_out_of_range_weights():
    """Weights summing to 1 but outside [0, 1] are rejected (no inverted priorities)."""
    with pytest.raises(ValueError):
        UserProfile("neg", 1.2, -0.1, -0.1)
    with pytest.raises(ValueError):
        UserProfile("over", 1.5, 0.5, -1.0)


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
    # 1 K excess overshoot → comfort drops by 0.25 (weight in comfort)
    assert comfort_score(candidate, oracle) == pytest.approx(0.75, abs=0.01)


def test_comfort_score_inf_settling_penalised():
    """Inf settling costs the full 0.25 settling weight."""
    oracle = _zero_metrics()
    candidate = _zero_metrics(settling_time_min=math.inf)
    assert comfort_score(candidate, oracle) == pytest.approx(0.75)


def test_comfort_score_discomfort_penalty():
    """Excess discomfort costs its share of the 0.5 weight."""
    oracle = _zero_metrics(discomfort_K_h=0.5)
    # 2 K·h beyond the oracle over 8 h = 0.25 K mean, half the 0.5 K
    # failure threshold → penalty 0.25.
    candidate = _zero_metrics(discomfort_K_h=2.5)
    assert comfort_score(candidate, oracle) == pytest.approx(0.75)


def test_comfort_score_discomfort_averages_over_at_least_four_hours():
    """A short run is averaged over four hours, so a brief event still weighs."""
    oracle = _zero_metrics(transient_duration_h=1.0)
    candidate = _zero_metrics(discomfort_K_h=1.0, transient_duration_h=1.0)
    # 1 K·h over 4 h = 0.25 K → penalty 0.5 · 0.5.
    assert comfort_score(candidate, oracle) == pytest.approx(0.75)


def test_comfort_score_less_discomfort_than_oracle_scores_one():
    oracle = _zero_metrics(discomfort_K_h=1.0)
    candidate = _zero_metrics(discomfort_K_h=0.2)
    assert comfort_score(candidate, oracle) == pytest.approx(1.0)


def test_comfort_score_clamps_to_zero():
    """Catastrophic comfort fails (discomfort + overshoot + settling) clamp at 0."""
    oracle = _zero_metrics()
    candidate = _zero_metrics(
        max_overshoot_K=10.0, settling_time_min=math.inf, discomfort_K_h=40.0
    )
    assert comfort_score(candidate, oracle) == pytest.approx(0.0)


# ---------- Actuator score ----------


def test_actuator_score_matches_oracle_returns_one():
    """Matching the oracle's travel scores 1.0."""
    oracle = _zero_metrics(total_valve_travel_percent=500.0)
    candidate = _zero_metrics(total_valve_travel_percent=500.0)
    assert actuator_score(candidate, oracle) == pytest.approx(1.0)


def test_actuator_score_5x_travel_scores_zero():
    """5× the oracle's travel scores 0."""
    oracle = _zero_metrics(total_valve_travel_percent=500.0)
    candidate = _zero_metrics(total_valve_travel_percent=2500.0)
    assert actuator_score(candidate, oracle) == pytest.approx(0.0)


def test_actuator_score_less_travel_than_oracle_scores_one():
    """Using *less* travel than the oracle still caps at 1.0."""
    oracle = _zero_metrics(total_valve_travel_percent=500.0)
    candidate = _zero_metrics(total_valve_travel_percent=100.0)
    assert actuator_score(candidate, oracle) == pytest.approx(1.0)


def test_actuator_score_handles_low_oracle_travel():
    """When the oracle barely moved, the candidate is compared against a floor."""
    oracle = _zero_metrics(total_valve_travel_percent=0.5)
    candidate = _zero_metrics(total_valve_travel_percent=250.0)
    # Floor of 500 → 250/500 = 0.5 penalty → score 0.5
    assert 0.4 < actuator_score(candidate, oracle) < 0.6


def test_actuator_score_is_continuous_as_oracle_travel_approaches_zero():
    """Oracle travel just above and just below 1 % gives the same score."""
    candidate = _zero_metrics(total_valve_travel_percent=36.0)
    below = actuator_score(candidate, _zero_metrics(total_valve_travel_percent=0.9))
    above = actuator_score(candidate, _zero_metrics(total_valve_travel_percent=1.3))
    assert below == pytest.approx(above, abs=0.01)
    assert above > 0.9


def test_actuator_score_against_idle_oracle_fails_at_500_percent():
    """An idle oracle still lets a candidate spend up to 500 % of travel."""
    idle = _zero_metrics(total_valve_travel_percent=0.0)
    assert actuator_score(
        _zero_metrics(total_valve_travel_percent=500.0), idle
    ) == pytest.approx(0.0)
    assert actuator_score(
        _zero_metrics(total_valve_travel_percent=250.0), idle
    ) == pytest.approx(0.5)


# ---------- Energy score ----------


def test_energy_score_matches_oracle_returns_one():
    """Matching the oracle's delivered heat scores 1.0."""
    oracle = _zero_metrics(delivered_heat_K=5.0)
    candidate = _zero_metrics(delivered_heat_K=5.0)
    assert energy_score(candidate, oracle) == pytest.approx(1.0)


def test_energy_score_double_oracle_scores_zero():
    """Delivering 2× the oracle's heat scores 0."""
    oracle = _zero_metrics(delivered_heat_K=5.0)
    candidate = _zero_metrics(delivered_heat_K=10.0)
    assert energy_score(candidate, oracle) == pytest.approx(0.0)


def test_energy_score_low_oracle_neutral_for_matching_candidate():
    """When the oracle barely heated, a candidate near it still scores ~1.0."""
    oracle = _zero_metrics(delivered_heat_K=0.02)
    candidate = _zero_metrics(delivered_heat_K=0.025)
    assert energy_score(candidate, oracle) == pytest.approx(1.0 - 0.005 / 0.1)


def test_energy_score_low_oracle_penalises_gross_overuse():
    """A grossly over-heating candidate must not escape via the low-oracle path."""
    oracle = _zero_metrics(delivered_heat_K=0.02)
    candidate = _zero_metrics(delivered_heat_K=2.0)
    # Excess of ~1.98 K against the 0.1 K floor → fully penalised.
    assert energy_score(candidate, oracle) == pytest.approx(0.0)


def test_energy_score_low_oracle_leaves_underheating_to_comfort():
    """Heating less than a barely-heating oracle costs no energy score."""
    oracle = _zero_metrics(delivered_heat_K=0.08)
    candidate = _zero_metrics(delivered_heat_K=0.0)
    assert energy_score(candidate, oracle) == pytest.approx(1.0)


def test_energy_score_leaves_underheating_to_comfort():
    """Heating less than the oracle is a missed setpoint, which comfort scores."""
    oracle = _zero_metrics(delivered_heat_K=5.0)
    for heat in (3.0, 0.0):
        candidate = _zero_metrics(delivered_heat_K=heat)
        assert energy_score(candidate, oracle) == pytest.approx(1.0)


def test_energy_score_charges_excess_heat_proportionally():
    oracle = _zero_metrics(delivered_heat_K=5.0)
    candidate = _zero_metrics(delivered_heat_K=6.0)
    assert energy_score(candidate, oracle) == pytest.approx(0.8)


def test_energy_score_ignores_the_commanded_valve_integral():
    """Energy follows the heat the plant delivered, not the valve command."""
    oracle = _zero_metrics(delivered_heat_K=5.0, integral_valve_percent_minutes=5000.0)
    candidate = _zero_metrics(
        delivered_heat_K=5.0, integral_valve_percent_minutes=10000.0
    )
    assert energy_score(candidate, oracle) == pytest.approx(1.0)


# ---------- compute_scores integration ----------


def test_compute_scores_returns_all_dimensions():
    """The aggregate function returns comfort, actuator, energy, overall."""
    oracle = _zero_metrics(total_valve_travel_percent=500.0, delivered_heat_K=5.0)
    candidate = _zero_metrics(
        max_overshoot_K=0.5, total_valve_travel_percent=750.0, delivered_heat_K=5.5
    )
    profile = PROFILES["balanced"]
    s = compute_scores(candidate, oracle, profile)
    assert 0.0 <= s.comfort <= 1.0
    assert 0.0 <= s.actuator <= 1.0
    assert 0.0 <= s.energy <= 1.0
    assert 0.0 <= s.overall <= 1.0


def test_overall_score_respects_profile_weights():
    """Same dimension scores under different profiles give different overalls."""
    oracle = _zero_metrics(total_valve_travel_percent=500.0)
    # Candidate: perfect comfort, terrible actuator
    candidate = _zero_metrics(total_valve_travel_percent=2500.0)
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
