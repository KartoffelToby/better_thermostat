"""Weighted scoring across the user-priority dimensions.

Each (controller, scenario) pair produces a continuous 0..1 score per
dimension (comfort, actuator longevity, energy) and a weighted aggregate
under a chosen ``UserProfile``.

Each dimension is normalised against the IdealOracle:

* Oracle's value → ``1.0`` (the reference; a candidate that beats it
  also scores ``1.0``)
* Controller value ≥ "failure" threshold → ``0.0``
* Linear interpolation in between

Failure thresholds are chosen so the score scale is interpretable:

* Comfort: weighted penalty of discomfort (0.5), overshoot (0.25) and
  settling (0.25). Discomfort is the deviation beyond the ±0.2 K band in
  K·h; it saturates at 0.5 K beyond the oracle's, averaged over the
  scored phase (at least 4 h). Overshoot saturates at 1 K extra,
  settling at 5× the oracle's. A single saturated component costs its
  weight; comfort reaches 0 only when all three saturate
* Actuator: a controller with ≥ 5× oracle's total valve travel scores ≤ 0;
  below a small travel floor the excess is scored against the floor
* Energy: heat delivered to the room beyond the oracle's — ≥ 2× the
  oracle's heat scores 0. Heating less is not scored here: the room
  then misses its setpoint, which comfort counts

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
        """Validate that each weight is in [0, 1] and the three sum to 1.0.

        Raises
        ------
        ValueError
            If any weight falls outside [0, 1], or the weights do not sum
            to 1.0 within tolerance.
        """
        weights = (self.w_comfort, self.w_actuator, self.w_energy)
        if any(w < 0.0 or w > 1.0 for w in weights):
            raise ValueError(
                f"UserProfile weights must each be in [0, 1], got {weights} for {self.name}"
            )
        s = sum(weights)
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


#: Mean deviation beyond the comfort band, in excess of the oracle's, at
#: which the discomfort component saturates; and the shortest phase it is
#: averaged over, so a brief event in a short run still weighs.
_DISCOMFORT_FAILURE_K = 0.5
_DISCOMFORT_MIN_DURATION_H = 4.0


def comfort_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score comfort across discomfort, overshoot and settling.

    Weights inside comfort: 0.5 discomfort, 0.25 overshoot, 0.25 settling.
    Failure thresholds: 0.5 K mean excess discomfort, +1 K overshoot, 5×
    oracle settling.

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
    discomfort_excess = max(0.0, metrics.discomfort_K_h - oracle.discomfort_K_h)
    duration_h = max(metrics.transient_duration_h, _DISCOMFORT_MIN_DURATION_H)
    discomfort_pen = _clamp_01(discomfort_excess / (_DISCOMFORT_FAILURE_K * duration_h))

    overshoot_excess = max(0.0, metrics.max_overshoot_K - oracle.max_overshoot_K)
    overshoot_pen = _clamp_01(overshoot_excess / 1.0)

    settling_ratio = _settling_ratio(
        metrics.settling_time_min, oracle.settling_time_min
    )
    settling_pen = _clamp_01((settling_ratio - 1.0) / 4.0)  # 1× → 0, 5× → 1

    penalty = 0.5 * discomfort_pen + 0.25 * overshoot_pen + 0.25 * settling_pen
    return 1.0 - penalty


#: Valve travel (Σ|Δu|, percent) below which an oracle's run counts as
#: practically idle. Excess travel is scored against this floor instead of
#: the oracle's own travel, so the score stays continuous as the oracle's
#: travel approaches zero: against an idle oracle a candidate reaches 0 at
#: four floors (500 %), against a busy one at five times the oracle.
_ACTUATOR_FLOOR_PCT = 125.0


def actuator_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score total valve travel relative to the oracle.

    Failure threshold: 4× the larger of the oracle's travel and
    ``_ACTUATOR_FLOOR_PCT`` in excess of the oracle's travel. Cycle count
    is *not* used directly — see DESIGN.md §5 (actuator metric):
    tracking-precise controllers (oracle) cycle many small times; total
    travel is the honest wear/battery proxy.

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
    excess = max(
        0.0, metrics.total_valve_travel_percent - oracle.total_valve_travel_percent
    )
    scale = 4.0 * max(oracle.total_valve_travel_percent, _ACTUATOR_FLOOR_PCT)
    return _clamp_01(1.0 - excess / scale)


#: Below this oracle heat the ratio is ill-conditioned (the scenario
#: barely heated), so the deviation is scored against this absolute floor
#: instead. A candidate that deviates by a full floor scores 0.
_ENERGY_FLOOR_K = 0.1


def energy_score(metrics: MetricValues, oracle: MetricValues) -> float:
    """Score heat delivered to the room beyond the oracle's.

    The oracle delivers the heat required to track the setpoint, so heat
    beyond it is waste: ≥ 2× the oracle's heat scores 0. Less heat than
    the oracle's is not a saving but a missed setpoint, which comfort
    scores; counting it here as well would charge it twice.

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
    if oracle.delivered_heat_K < _ENERGY_FLOOR_K:
        # Oracle barely heated, so the ratio is ill-conditioned. Score the
        # candidate's absolute excess over the oracle against the floor
        # rather than treating every candidate as oracle-equivalent.
        excess = max(0.0, metrics.delivered_heat_K - oracle.delivered_heat_K)
        return _clamp_01(1.0 - excess / _ENERGY_FLOOR_K)
    ratio = metrics.delivered_heat_K / oracle.delivered_heat_K
    return _clamp_01(1.0 - max(0.0, ratio - 1.0))


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
