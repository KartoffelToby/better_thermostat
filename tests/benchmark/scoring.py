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
    """Sub-scores per dimension, plus the weighted overall score.

    Attributes
    ----------
    comfort : float
        Comfort sub-score in 0..1.
    actuator : float
        Actuator-longevity sub-score in 0..1.
    energy : float
        Energy sub-score in 0..1.
    overall : float
        Profile-weighted aggregate of the three sub-scores.
    """

    comfort: float
    actuator: float
    energy: float
    overall: float


@dataclass(frozen=True)
class UserProfile:
    """Weights expressing the user's relative priorities.

    The three weights must sum to 1. ``balanced`` is the safe default;
    the other profiles bias the score toward a specific axis.

    Attributes
    ----------
    name : str
        Profile identifier used in reports and the CLI.
    w_comfort : float
        Weight of the comfort dimension.
    w_actuator : float
        Weight of the actuator-longevity dimension.
    w_energy : float
        Weight of the energy dimension.
    """

    name: str
    w_comfort: float
    w_actuator: float
    w_energy: float

    def __post_init__(self) -> None:
        """Validate that the three weights sum to 1.0.

        Raises
        ------
        ValueError
            If the weights do not sum to 1.0 within tolerance.
        """
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


# When the oracle settles instantly there is no transient to normalise
# against; a candidate consuming this many minutes still earns the full
# settling penalty.
_ABS_SETTLING_FAILURE_MIN = 5.0


def _settling_ratio(metric: float, oracle: float) -> float:
    """Settling-time ratio with sensible inf handling.

    Parameters
    ----------
    metric : float
        Candidate controller's settling time in minutes (may be inf).
    oracle : float
        Oracle's settling time in minutes (may be inf).

    Returns
    -------
    float
        Ratio ``metric / oracle``; capped at 10 when only the candidate
        never settles, and computed against an absolute scale when the
        oracle settled instantly.
    """
    if math.isinf(metric) and math.isinf(oracle):
        return 1.0
    if math.isinf(metric):
        return 10.0
    if math.isinf(oracle):
        return 1.0
    if oracle <= 0.0:
        # Zero-settling oracle: score the candidate on an absolute scale
        # instead of treating every settling time as oracle-equivalent.
        return 1.0 + _clamp_01(metric / _ABS_SETTLING_FAILURE_MIN) * 4.0
    return metric / oracle


def comfort_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score comfort across overshoot, settling and steady-state error.

    Weights inside comfort: 0.4 overshoot, 0.4 settling, 0.2 ss_error.
    Failure thresholds: +1 K overshoot, 5× oracle settling, +0.5 K ss_error.

    Parameters
    ----------
    metrics : MetricValues
        Candidate controller's metrics.
    oracle : MetricValues
        Oracle baseline metrics for the same scenario.

    Returns
    -------
    float
        Comfort score; 1.0 is oracle-equivalent, 0.0 is at/beyond the
        failure thresholds.
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
    """Score total valve travel relative to the oracle.

    Failure threshold: 5× oracle's total travel. Cycle count is *not*
    used directly — see DESIGN.md §5 (actuator metric): tracking-precise
    controllers (oracle) cycle many small times; total travel is the
    honest wear/battery proxy.

    Parameters
    ----------
    metrics : MetricValues
        Candidate controller's metrics.
    oracle : MetricValues
        Oracle baseline metrics for the same scenario.

    Returns
    -------
    float
        Actuator score in 0..1.
    """
    if oracle.total_valve_travel_pct < 1.0:
        # Oracle barely moved. Compare against a floor so a low-activity
        # controller still scores around 1.0.
        return _clamp_01(1.0 - metrics.total_valve_travel_pct / 500.0)
    ratio = metrics.total_valve_travel_pct / oracle.total_valve_travel_pct
    penalty = max(0.0, (ratio - 1.0) / 4.0)  # 1× → 0, 5× → 1
    return _clamp_01(1.0 - penalty)


#: Below this oracle integral the ratio is ill-conditioned (the scenario
#: barely heated/cooled), so excess usage is scored against this absolute
#: floor instead. A candidate that over-uses by a full floor scores 0.
_ENERGY_FLOOR_PCT_MIN = 100.0


def energy_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score integral valve usage relative to the oracle (symmetric).

    The oracle delivers exactly the heat required to track the setpoint, so
    any deviation is waste: ≥ 2× the oracle's integral is over-heating
    (overshoot losses), ≤ 0 is under-heating (setpoint missed — the same
    logic applies in cooling). Both directions are penalised equally.

    Parameters
    ----------
    metrics : MetricValues
        Candidate controller's metrics.
    oracle : MetricValues
        Oracle baseline metrics for the same scenario.

    Returns
    -------
    float
        Energy score in 0..1.
    """
    if oracle.integral_valve_pct_min < _ENERGY_FLOOR_PCT_MIN:
        # Oracle barely moved, so the ratio is ill-conditioned. Score the
        # candidate's *excess* usage over the oracle against the floor
        # rather than treating every candidate as oracle-equivalent —
        # otherwise a grossly over-heating controller escapes scoring here.
        excess = max(
            0.0, metrics.integral_valve_pct_min - oracle.integral_valve_pct_min
        )
        return _clamp_01(1.0 - excess / _ENERGY_FLOOR_PCT_MIN)
    ratio = metrics.integral_valve_pct_min / oracle.integral_valve_pct_min
    deviation = abs(ratio - 1.0)
    return _clamp_01(1.0 - deviation)


def compute_scores(
    metrics: MetricValues, oracle: MetricValues, profile: UserProfile
) -> DimensionScores:
    """Compute all three sub-scores plus the profile-weighted overall.

    Parameters
    ----------
    metrics : MetricValues
        Candidate controller's metrics.
    oracle : MetricValues
        Oracle baseline metrics for the same scenario.
    profile : UserProfile
        Weighting profile for the overall aggregate.

    Returns
    -------
    DimensionScores
        Sub-scores and the weighted overall score.
    """
    c = comfort_score(metrics, oracle)
    a = actuator_score(metrics, oracle)
    e = energy_score(metrics, oracle)
    overall = profile.w_comfort * c + profile.w_actuator * a + profile.w_energy * e
    return DimensionScores(comfort=c, actuator=a, energy=e, overall=overall)
