"""Disturbance observer — EMA over the Kalman filter's room corrections.

Each update receives the amount the filter moved its room estimate towards
the measurement. Under a standing disturbance that correction is what keeps
the estimate on the measured room, so its rate is the disturbance rate. The
raw innovation is not: the filter applies only its room gain of it, and the
remainder reappears in the next innovation.

The QP's steady-state input ``u_ss`` assumes the plant model is exact. Real
rooms see unmodelled disturbances (open windows, solar gain, occupants);
the DOB captures the average rate in ``K/min`` so ``_steady_input_for`` can
feed-forward against it. Without the DOB, integral-only correction would
leave a slow setpoint offset.

The optimiser plans with a second, slower reading of the same estimate,
``planning_rate``. It carries the rate along its whole horizon, where an
error of 0.003 K/min already shifts the predicted room by about 0.2 K and
the valve by tens of percent; the fast estimate carries that much from
sensor noise or quantisation alone.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

# Interval of Better Thermostat's periodic tick that recomputes balance and
# calibration (the five-minute timer in ``climate.py``). A calibration mode
# sees a control cycle at least this often while it runs.
CONTROL_TICK_S = 300.0

# A reading closes a regular control cycle up to this many control ticks
# after the previous one. Half a tick absorbs scheduling jitter and the
# event-driven cycles in between, which only ever shorten the interval; one
# skipped tick already means the controller did not run.
_READING_INTERVAL_TICKS = 1.5


@dataclass
class DobParams:
    """Tunables for the disturbance observer (EMA time constant)."""

    tau_s: float = 600.0
    # A single quantised room-sensor jump must not become an arbitrarily large
    # permanent heat source/sink in the steady-state feed-forward term.  0.05
    # K/min is already 3 K/hour, well beyond a normal unmodelled room load.
    max_abs_K_per_min: float = 0.05
    # Time constant (s) of the planning reading, and the band around zero
    # (K/min) it treats as no disturbance. Rates inside the band are what
    # sensor noise and quantisation produce on their own. A slower reading
    # calms the valve further but learns and forgets a sunny afternoon too
    # late, overheating the room while the sun holds and chilling it after.
    planning_tau_s: float = 1200.0
    planning_deadband: float = 0.002
    # Better Thermostat recomputes at least once per ``CONTROL_TICK_S`` while
    # a calibration mode is active. A longer interval (s) means the controller
    # did not run (window open, heating off, restart); the correction that
    # closes it measures that pause, not a standing disturbance.
    max_reading_interval_s: float = _READING_INTERVAL_TICKS * CONTROL_TICK_S


class DisturbanceObserver:
    """EMA over Kalman room corrections estimating the disturbance rate in K/min."""

    def __init__(self, params: DobParams) -> None:
        """Initialise the observer with a zero disturbance estimate.

        Parameters
        ----------
        params : DobParams
            Tunables for the observer, notably the EMA time constant ``tau_s``.
        """
        self.params = params
        self.D_hat_K_per_min: float = 0.0
        # Slow EMA of the estimate, before the deadband. Persisted.
        self.planning_filtered: float = 0.0

    @property
    def planning_rate(self) -> float:
        """Return the disturbance rate (K/min) the optimiser plans with.

        The slow EMA of the estimate, shrunk towards zero by
        ``planning_deadband`` so the result stays continuous.
        """
        band = max(0.0, self.params.planning_deadband)
        rate = self.planning_filtered
        if abs(rate) <= band:
            return 0.0
        return rate - math.copysign(band, rate)

    def restore(self, estimate: float, planning: float | None) -> None:
        """Adopt persisted estimates, bounded by ``max_abs_K_per_min``.

        A missing planning reading starts from zero rather than from the fast
        estimate: that one follows the last few readings, and the free heat
        they saw before a restart is no evidence for the plan after it.
        """
        max_abs = max(0.0, self.params.max_abs_K_per_min)
        self.D_hat_K_per_min = max(-max_abs, min(max_abs, estimate))
        self.planning_filtered = (
            0.0 if planning is None else max(-max_abs, min(max_abs, planning))
        )

    def update(self, correction_K: float, dt_s: float) -> float:
        """Fold one room correction into the EMA and return the disturbance estimate.

        Converts the per-step correction into a ``K/min`` rate and blends it
        with EMA weight ``a`` derived from ``dt_s`` and ``tau_s``. A
        non-positive ``dt_s``, or one beyond ``max_reading_interval_s``,
        leaves both estimates unchanged.

        The weight scales linearly with ``dt_s`` (no lower floor): the
        correction rate grows as ``1/dt_s``, so a dt-proportional weight keeps
        the per-update contribution ``a * correction_rate`` bounded by
        ``60 * correction_K / tau_s`` even for near-zero intervals, as they
        occur when a shared group controller is stepped once per TRV within
        the same control pass.

        The estimate itself is bounded by ``max_abs_K_per_min`` so a quantised
        sensor jump cannot become an implausible steady-state load. That bound
        belongs on the estimate rather than on the incoming rate: clamping the
        rate first would scale it by the dt-proportional weight as well, which
        drops the short-interval corrections this observer is meant to fold in.
        """
        if dt_s <= 0.0 or dt_s > self.params.max_reading_interval_s:
            return self.D_hat_K_per_min
        correction_rate = correction_K / (dt_s / 60.0)
        a = min(1.0, dt_s / max(self.params.tau_s, dt_s))
        max_abs = max(0.0, self.params.max_abs_K_per_min)
        self.D_hat_K_per_min = (1.0 - a) * self.D_hat_K_per_min + a * correction_rate
        self.D_hat_K_per_min = max(-max_abs, min(max_abs, self.D_hat_K_per_min))
        b = min(1.0, dt_s / max(self.params.planning_tau_s, dt_s))
        self.planning_filtered += b * (self.D_hat_K_per_min - self.planning_filtered)
        return self.D_hat_K_per_min
