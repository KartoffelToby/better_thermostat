"""Disturbance observer — the disturbance the optimiser and governor plan with.

The Kalman filter estimates the room's disturbance rate (K/min) as a state of
its own. This observer holds the two readings of it the controller uses: the
fast estimate, bounded by ``bound_K_per_min``, which the governor judges
reachability with, and ``planning_rate``, a slower filtered copy the optimiser
carries along its horizon. The QP's steady-state input ``u_ss`` assumes the
plant model is exact; feeding the disturbance forward removes the setpoint
offset an unmodelled load (sun, occupants, a wrong prior) would leave.

The optimiser needs the slower reading because it carries the rate along its
whole horizon, where an error of 0.003 K/min already shifts the predicted room
by about 0.2 K and the valve by tens of percent; the fast estimate carries
that much from sensor noise or quantisation alone.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

from custom_components.better_thermostat.core.watchdog import CONTROL_TICK_S

# A reading closes a regular control cycle up to this many control ticks
# after the previous one. Half a tick absorbs scheduling jitter and the
# event-driven cycles in between, which only ever shorten the interval; one
# skipped tick already means the controller did not run.
_READING_INTERVAL_TICKS = 1.5


@dataclass
class DobParams:
    """Tunables for the disturbance observer."""

    # A single quantised room-sensor jump must not become an arbitrarily large
    # permanent heat source/sink in the steady-state feed-forward term.  0.05
    # K/min is already 3 K/hour, well beyond a normal unmodelled room load.
    max_abs_K_per_min: float = 0.05
    # The widest bound as a shift of the room's heat balance, in K. A rate
    # ``D`` carries ``D·τ_room`` of loss-equivalent temperature into the
    # feed-forward, so a fast prior needs a larger rate to cover the same
    # model error: a missing outdoor sensor 15 K off the real outdoor
    # temperature is 0.083 K/min at τ_room = 180 min. 24 K matches
    # ``max_abs_K_per_min`` at the default τ_room of 480 min.
    max_abs_loss_K: float = 24.0
    # The bound starts at ``max_abs_K_per_min`` and widens towards the
    # ``max_abs_loss_K`` ceiling only while the room is settled: the
    # setpoint and the controller have run without a change or a pause for
    # ``settle_s``, and over that span the room has moved by no more than
    # ``settle_slope_K_per_h`` would carry it. It then grows by ``bound_growth_K_per_h`` of
    # loss-equivalent temperature per hour. A transient (a setpoint step, the
    # recovery from airing) runs on the narrow bound, where a wrong prior's
    # dynamics would otherwise pass for a standing load.
    settle_s: float = 3600.0
    settle_slope_K_per_h: float = 0.5
    bound_growth_K_per_h: float = 6.0
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

    def bound_K_per_min(self, tau_room_min: float) -> float:
        """Return the widest disturbance bound (K/min) for a room time constant.

        The larger of ``max_abs_K_per_min`` and ``max_abs_loss_K / τ_room``,
        so a slow prior keeps the rate bound and a fast one may widen to the
        rate that covers ``max_abs_loss_K`` of model error.
        """
        rate_bound = max(0.0, self.max_abs_K_per_min)
        if tau_room_min <= 0.0:
            return rate_bound
        return max(rate_bound, max(0.0, self.max_abs_loss_K) / tau_room_min)


class DisturbanceObserver:
    """Bounded and slowly filtered readings of the Kalman disturbance (K/min)."""

    def __init__(self, params: DobParams, tau_room_min: float | None = None) -> None:
        """Initialise the observer with a zero disturbance estimate.

        Parameters
        ----------
        params : DobParams
            Tunables for the observer, notably the bound and the planning
            time constant.
        tau_room_min : float | None, optional
            The prior's room time constant, which sets how far the bound may
            widen. ``None`` keeps it at ``params.max_abs_K_per_min``.
        """
        self.params = params
        self.floor_K_per_min = max(0.0, params.max_abs_K_per_min)
        self.ceiling_K_per_min = (
            self.floor_K_per_min
            if tau_room_min is None
            else params.bound_K_per_min(tau_room_min)
        )
        self.tau_room_min = tau_room_min
        # Largest estimate the observer passes on, in either sign.
        self.bound_K_per_min = self.floor_K_per_min
        self.D_hat_K_per_min: float = 0.0
        # Slow EMA of the estimate, before the deadband. Persisted.
        self.planning_filtered: float = 0.0
        # Setpoint and room readings since the room last counted as unsettled.
        self._settled_setpoint: float | None = None
        self._recent_rooms: deque[tuple[float, float]] = deque()
        self._settled_for_s: float = 0.0

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
        """Adopt persisted estimates, bounded by the ceiling.

        The bound reopens as far as the larger of the two needs. A missing
        planning reading starts from zero rather than from the fast estimate:
        that one follows the last few readings, and the free heat they saw
        before a restart is no evidence for the plan after it.
        """
        self.bound_K_per_min = self.ceiling_K_per_min
        self.D_hat_K_per_min = self._bounded(estimate)
        self.planning_filtered = 0.0 if planning is None else self._bounded(planning)
        self._narrow_to_the_estimates()

    def track_settling(
        self, t_s: float, room: float, setpoint: float, paused: bool
    ) -> float:
        """Widen or narrow the bound for this cycle and return it.

        A changed setpoint or a pause narrows the bound to what the current
        estimates already use, so a learned load is kept but none is added
        during the transient that follows. While the room is settled the
        bound grows at ``bound_growth_K_per_h`` of loss-equivalent
        temperature per hour, up to the ceiling.
        """
        p = self.params
        if paused or setpoint != self._settled_setpoint:
            self._settled_setpoint = setpoint
            self._recent_rooms.clear()
            self._recent_rooms.append((t_s, room))
            self._settled_for_s = 0.0
            self._narrow_to_the_estimates()
            return self.bound_K_per_min
        last_t_s = self._recent_rooms[-1][0] if self._recent_rooms else t_s
        dt_s = max(0.0, t_s - last_t_s)
        self._recent_rooms.append((t_s, room))
        while len(self._recent_rooms) > 1 and (
            t_s - self._recent_rooms[1][0] >= p.settle_s
        ):
            self._recent_rooms.popleft()
        oldest_room = self._recent_rooms[0][1]
        moving = abs(room - oldest_room) > p.settle_slope_K_per_h * p.settle_s / 3600.0
        self._settled_for_s = 0.0 if moving else self._settled_for_s + dt_s
        if self._settled_for_s >= p.settle_s and self.tau_room_min:
            growth = p.bound_growth_K_per_h / 60.0 / self.tau_room_min
            self.bound_K_per_min = min(
                self.ceiling_K_per_min, self.bound_K_per_min + growth * dt_s / 60.0
            )
        return self.bound_K_per_min

    def _narrow_to_the_estimates(self) -> None:
        needed = max(abs(self.D_hat_K_per_min), abs(self.planning_filtered))
        self.bound_K_per_min = min(
            self.ceiling_K_per_min, max(self.floor_K_per_min, needed)
        )

    def follow(self, estimate_K_per_min: float, dt_s: float) -> float:
        """Take the filter's disturbance estimate and return the bounded one.

        The planning reading moves towards it with weight
        ``dt_s / planning_tau_s``. A non-positive ``dt_s``, or one beyond
        ``max_reading_interval_s``, leaves the planning reading unchanged:
        that interval spans a pause of the controller.
        """
        self.D_hat_K_per_min = self._bounded(estimate_K_per_min)
        if 0.0 < dt_s <= self.params.max_reading_interval_s:
            b = min(1.0, dt_s / max(self.params.planning_tau_s, dt_s))
            self.planning_filtered += b * (
                self.D_hat_K_per_min - self.planning_filtered
            )
        return self.D_hat_K_per_min

    def _bounded(self, rate_K_per_min: float) -> float:
        max_abs = self.bound_K_per_min
        return max(-max_abs, min(max_abs, rate_K_per_min))
