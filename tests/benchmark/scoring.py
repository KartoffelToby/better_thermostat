"""Weighted scoring across the user-priority dimensions.

Each (controller, scenario) pair produces a continuous 0..1 score per
dimension (comfort, actuator longevity, energy) and a weighted aggregate
under a chosen ``UserProfile``.

Each dimension is normalised against the IdealOracle:

* Oracle's value → ``1.0`` (best physically reachable)
* Controller value ≥ "failure" threshold → ``0.0``
* Linear interpolation in between

Failure thresholds are chosen so the score scale is interpretable:

* Comfort: a controller with ≥ 1 K extra overshoot, ≥ 5× oracle settling
  or ≥ 0.5 K extra steady-state error scores ≤ 0
* Actuator: a controller with ≥ 5× oracle's total valve travel scores ≤ 0
* Energy: symmetric — ≥ 2× oracle's integral is over-heating,
  ≤ 0× is under-heating; both directions score ≤ 0

Resilience is not a continuous per-run dimension — it shows up as
catastrophic comfort/actuator scores in the edge-case scenarios
(sensor dropout, large outdoor steps, etc.). It is captured implicitly
by averaging across all scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .metrics import MetricValues


@dataclass(frozen=True)
class DimensionScores:
    """Sub-scores per dimension, plus the weighted overall score."""

    comfort: float
    actuator: float
    energy: float
    overall: float


@dataclass(frozen=True)
class UserProfile:
    """Weights expressing the user's relative priorities.

    The three weights must sum to 1. ``balanced`` is the safe default;
    the other profiles bias the score toward a specific axis.
    """

    name: str
    w_comfort: float
    w_actuator: float
    w_energy: float

    def __post_init__(self) -> None:
        """Validate that the three weights sum to 1.0."""
        s = self.w_comfort + self.w_actuator + self.w_energy
        if not math.isclose(s, 1.0, abs_tol=1e-3):
            raise ValueError(
                f"UserProfile weights must sum to 1.0, got {s:.4f} for {self.name}"
            )


PROFILES: dict[str, UserProfile] = {
    "balanced": UserProfile("balanced", 0.50, 0.30, 0.20),
    "comfort_first": UserProfile("comfort_first", 0.75, 0.15, 0.10),
    "longevity_first": UserProfile("longevity_first", 0.30, 0.55, 0.15),
    "energy_first": UserProfile("energy_first", 0.30, 0.15, 0.55),
}


# --- Sub-score helpers ---


def _clamp_01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _settling_ratio(metric: float, oracle: float) -> float:
    """Settling-time ratio with sensible inf handling."""
    if math.isinf(metric) and math.isinf(oracle):
        return 1.0
    if math.isinf(metric):
        return 10.0
    if math.isinf(oracle) or oracle <= 0.0:
        return 1.0
    return metric / oracle


def comfort_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score 0..1 across overshoot, settling and steady-state error.

    Weights inside comfort: 0.4 overshoot, 0.4 settling, 0.2 ss_error.
    Failure thresholds: +1 K overshoot, 5× oracle settling, +0.5 K ss_error.
    """
    overshoot_excess = max(0.0, metrics.max_overshoot_K - oracle.max_overshoot_K)
    overshoot_pen = _clamp_01(overshoot_excess / 1.0)

    settling_ratio = _settling_ratio(
        metrics.settling_time_min, oracle.settling_time_min
    )
    settling_pen = _clamp_01((settling_ratio - 1.0) / 4.0)  # 1× → 0, 5× → 1

    ss_excess = max(0.0, metrics.steady_state_error_K - oracle.steady_state_error_K)
    ss_pen = _clamp_01(ss_excess / 0.5)

    penalty = 0.4 * overshoot_pen + 0.4 * settling_pen + 0.2 * ss_pen
    return 1.0 - penalty


def actuator_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score 0..1 — total valve travel relative to oracle.

    Failure threshold: 5× oracle's total travel. Cycle count is *not*
    used directly — see WHITEPAPER §13 / reflexion: tracking-precise
    controllers (oracle) cycle many small times; total travel is the
    honest wear/battery proxy.
    """
    if oracle.total_valve_travel_pct < 1.0:
        # Oracle barely moved. Compare against a floor so a low-activity
        # controller still scores around 1.0.
        return _clamp_01(1.0 - metrics.total_valve_travel_pct / 500.0)
    ratio = metrics.total_valve_travel_pct / oracle.total_valve_travel_pct
    penalty = max(0.0, (ratio - 1.0) / 4.0)  # 1× → 0, 5× → 1
    return _clamp_01(1.0 - penalty)


def energy_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score 0..1 — integral valve usage relative to oracle (symmetric).

    The oracle delivers exactly the heat required to track the setpoint, so
    any deviation is waste: ≥ 2× the oracle's integral is over-heating
    (overshoot losses), ≤ 0 is under-heating (setpoint missed — the same
    logic applies in cooling). Both directions are penalised equally.
    """
    if oracle.integral_valve_pct_min < 100.0:
        # Edge case — scenario barely heated/cooled; treat as neutral.
        return 1.0
    ratio = metrics.integral_valve_pct_min / oracle.integral_valve_pct_min
    deviation = abs(ratio - 1.0)
    return _clamp_01(1.0 - deviation)


def compute_scores(
    metrics: MetricValues, oracle: MetricValues, profile: UserProfile
) -> DimensionScores:
    """Return all three sub-scores plus the profile-weighted overall."""
    c = comfort_score(metrics, oracle)
    a = actuator_score(metrics, oracle)
    e = energy_score(metrics, oracle)
    overall = profile.w_comfort * c + profile.w_actuator * a + profile.w_energy * e
    return DimensionScores(comfort=c, actuator=a, energy=e, overall=overall)
