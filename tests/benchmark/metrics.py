"""Metric computations over a benchmark time-series.

All metrics work on a :class:`TimeSeries` of parallel arrays. Time-series
samples are assumed equally spaced in coarse steps; metrics that need
specific resolution document their assumptions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class TimeSeries:
    """Parallel arrays of recorded simulator state at every step."""

    t_s: Sequence[float]
    T_room_C: Sequence[float]
    T_setpoint_C: Sequence[float]
    valve_pct: Sequence[float]

    def __post_init__(self) -> None:
        """Validate the parallel-array invariants every metric relies on.

        Raises
        ------
        ValueError
            If the four sequences differ in length or ``t_s`` is not
            strictly increasing.
        """
        lengths = {
            len(self.t_s),
            len(self.T_room_C),
            len(self.T_setpoint_C),
            len(self.valve_pct),
        }
        if len(lengths) != 1:
            raise ValueError("TimeSeries fields must have the same length")
        if any(t2 <= t1 for t1, t2 in zip(self.t_s, self.t_s[1:])):
            raise ValueError("TimeSeries.t_s must be strictly increasing")


@dataclass(frozen=True)
class MetricValues:
    """Computed metrics for one (controller, scenario) run.

    The metrics span four of the five user-priority dimensions described
    in DESIGN.md §4 (the fifth — adaptation across runs — is a
    benchmark-suite-level metric, not a per-run one):

    * **Comfort**: max_overshoot_K, max_undershoot_K, rmse_tracking_K,
      steady_state_error_K, settling_time_min
    * **Actuator longevity**: valve_cycle_count, total_valve_travel_pct
    * **Energy**: integral_valve_pct_min
    * **Resilience**: implicit in failure-mode metrics (settling=inf etc.)
    """

    max_overshoot_K: float
    max_undershoot_K: float
    settling_time_min: float  # math.inf if not settled within run
    steady_state_error_K: float
    rmse_tracking_K: float
    valve_cycle_count: int
    integral_valve_pct_min: float
    # Actuator-longevity proxy: Σ|Δu_pct| across the whole run, so small
    # wiggles add up even if they never reverse direction.
    total_valve_travel_pct: float
    # Asymmetric comfort accounting in K·h (BOPTEST tdis_tot split), useful
    # where overshoot and undershoot have different cost. Integrated over
    # the transient phase only.
    time_above_setpoint_K_h: float
    time_below_setpoint_K_h: float
    # Fraction of run time spent with the valve at 40–60 %. Heat-pump
    # COP suffers at the extremes; the sweet spot is mid-range modulation.
    valve_sweet_spot_residency_pct: float


def _compute_overshoot(
    series: TimeSeries, transient_start_s: float
) -> tuple[float, float]:
    over, under = 0.0, 0.0
    for t, T, sp in zip(series.t_s, series.T_room_C, series.T_setpoint_C):
        if t < transient_start_s:
            continue
        if T > sp + over:
            over = T - sp
        if T < sp - under:
            under = sp - T
    return over, under


def _compute_settling(
    series: TimeSeries,
    transient_start_s: float,
    band_K: float = 0.2,
    min_dwell_s: float = 600.0,
) -> float:
    """Return settling time in minutes.

    Minutes from the transient start until ``|T - setpoint| < band_K`` stays
    true for at least ``min_dwell_s`` continuous seconds. Returns inf if the
    settling never occurs within the run.
    """
    in_band_since: float | None = None
    for t, T, sp in zip(series.t_s, series.T_room_C, series.T_setpoint_C):
        if t < transient_start_s:
            continue
        if abs(T - sp) < band_K:
            if in_band_since is None:
                in_band_since = t
            elif (t - in_band_since) >= min_dwell_s:
                return (in_band_since - transient_start_s) / 60.0
        else:
            in_band_since = None
    return math.inf


def _compute_steady_state(series: TimeSeries, window_min: float = 30.0) -> float:
    """Mean absolute tracking error over the final ``window_min`` minutes."""
    if not series.t_s:
        return 0.0
    final_t = series.t_s[-1]
    window_start = final_t - window_min * 60.0
    errs = [
        abs(T - sp)
        for t, T, sp in zip(series.t_s, series.T_room_C, series.T_setpoint_C)
        if t >= window_start
    ]
    if not errs:
        return 0.0
    return sum(errs) / len(errs)


def _compute_rmse(series: TimeSeries, transient_start_s: float) -> float:
    errs = [
        (T - sp) ** 2
        for t, T, sp in zip(series.t_s, series.T_room_C, series.T_setpoint_C)
        if t >= transient_start_s
    ]
    if not errs:
        return 0.0
    return math.sqrt(sum(errs) / len(errs))


def _compute_valve_cycles(series: TimeSeries) -> int:
    """Count valve-direction reversals across the run."""
    cycles = 0
    last_direction = 0
    for i in range(1, len(series.valve_pct)):
        delta = series.valve_pct[i] - series.valve_pct[i - 1]
        direction = 1 if delta > 0.5 else (-1 if delta < -0.5 else 0)
        if direction != 0 and last_direction not in (0, direction):
            cycles += 1
        if direction != 0:
            last_direction = direction
    return cycles


def _compute_integral_valve(series: TimeSeries) -> float:
    """Trapezoidal integral of valve_pct over time (units: pct·min)."""
    if len(series.t_s) < 2:
        return 0.0
    total = 0.0
    for i in range(1, len(series.t_s)):
        dt_min = (series.t_s[i] - series.t_s[i - 1]) / 60.0
        avg_pct = (series.valve_pct[i] + series.valve_pct[i - 1]) / 2.0
        total += avg_pct * dt_min
    return total


def _compute_setpoint_imbalance_K_h(
    series: TimeSeries, transient_start_s: float
) -> tuple[float, float]:
    """Return (∫max(T-SP,0) dt, ∫max(SP-T,0) dt) in K·h over the transient phase."""
    if len(series.t_s) < 2:
        return 0.0, 0.0
    above_K_h = 0.0
    below_K_h = 0.0
    for i in range(1, len(series.t_s)):
        seg_end = series.t_s[i]
        if seg_end <= transient_start_s:
            continue
        # Clip the first interval so pre-transient time is not charged.
        seg_start = max(series.t_s[i - 1], transient_start_s)
        dt_h = (seg_end - seg_start) / 3600.0
        err = series.T_room_C[i] - series.T_setpoint_C[i]
        if err > 0.0:
            above_K_h += err * dt_h
        else:
            below_K_h += -err * dt_h
    return above_K_h, below_K_h


def _compute_valve_sweet_spot_residency(
    series: TimeSeries, low_pct: float = 40.0, high_pct: float = 60.0
) -> float:
    """Fraction of run time the commanded valve sits inside [low, high] %."""
    if not series.valve_pct:
        return 0.0
    inside = sum(1 for v in series.valve_pct if low_pct <= v <= high_pct)
    return 100.0 * inside / len(series.valve_pct)


def _compute_total_valve_travel(series: TimeSeries) -> float:
    """Σ|Δu_pct| over the whole run — actuator-wear/battery proxy."""
    if len(series.valve_pct) < 2:
        return 0.0
    return sum(
        abs(series.valve_pct[i] - series.valve_pct[i - 1])
        for i in range(1, len(series.valve_pct))
    )


def compute_metrics(series: TimeSeries, transient_start_s: float) -> MetricValues:
    """Compute all defined metrics for a single time-series."""
    over, under = _compute_overshoot(series, transient_start_s)
    above_K_h, below_K_h = _compute_setpoint_imbalance_K_h(series, transient_start_s)
    return MetricValues(
        max_overshoot_K=over,
        max_undershoot_K=under,
        settling_time_min=_compute_settling(series, transient_start_s),
        steady_state_error_K=_compute_steady_state(series),
        rmse_tracking_K=_compute_rmse(series, transient_start_s),
        valve_cycle_count=_compute_valve_cycles(series),
        integral_valve_pct_min=_compute_integral_valve(series),
        total_valve_travel_pct=_compute_total_valve_travel(series),
        time_above_setpoint_K_h=above_K_h,
        time_below_setpoint_K_h=below_K_h,
        valve_sweet_spot_residency_pct=_compute_valve_sweet_spot_residency(series),
    )
