"""Reporting threshold of the room sensor, learned from the readings.

A room sensor reports when its value has moved by a threshold since the last
report: 0.1 K for a fine sensor, 0.5 K or more for coarse ones, plus a
periodic heartbeat. Between reports the controller sees the same value again.
The threshold says how far the room may have moved since; Better Thermostat
does not know it, so this tracker estimates it from the steps between
consecutive distinct readings. A step the threshold triggers is the threshold
itself; a heartbeat reports a smaller one, and in a room that holds its
temperature nearly every step is a heartbeat's. The estimate is therefore a
high quantile of the steps, not their median. Steps above the upper bound are
room events, such as an opened window, and stay out of it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import math

# The bounds of the threshold estimate, in K. Below the lower one a sensor
# reports finer than the room's own noise; above the upper one a step is a
# room event, not the sensor's resolution.
MIN_REPORT_THRESHOLD_K = 0.1
MAX_REPORT_THRESHOLD_K = 1.0

# Number of recent steps the estimate runs over.
REPORT_STEP_WINDOW = 20

# Quantile of the recent steps taken as the threshold: high enough for the
# few triggered steps among many heartbeat steps, below the maximum so that a
# single odd step does not set it.
REPORT_STEP_QUANTILE = 0.9

# Steps needed before the estimate counts. A single step may be an opened
# window or the switch to the TRVs' readings when the room sensor fails.
MIN_REPORT_STEPS = 3


class ReportBand:
    """Tracks the last room reading and the steps between distinct readings."""

    def __init__(self) -> None:
        """Start without a reading and without any observed step."""
        self.last_reading: float | None = None
        self._steps: deque[float] = deque(maxlen=REPORT_STEP_WINDOW)

    @property
    def steps(self) -> list[float]:
        """Return the recent steps, oldest first."""
        return list(self._steps)

    @property
    def threshold_K(self) -> float:
        """Return the estimated reporting threshold (K).

        Until ``MIN_REPORT_STEPS`` steps are seen the estimate is the lower
        bound, which treats a repeated reading almost like a fresh one.
        """
        if len(self._steps) < MIN_REPORT_STEPS:
            return MIN_REPORT_THRESHOLD_K
        steps = sorted(step for step in self._steps if step <= MAX_REPORT_THRESHOLD_K)
        if not steps:
            return MAX_REPORT_THRESHOLD_K
        position = REPORT_STEP_QUANTILE * (len(steps) - 1)
        lower = math.floor(position)
        upper = min(lower + 1, len(steps) - 1)
        quantile = steps[lower] + (position - lower) * (steps[upper] - steps[lower])
        return max(MIN_REPORT_THRESHOLD_K, quantile)

    @property
    def move_suppression_factor(self) -> float:
        """Return the factor on the plan's weight against valve moves.

        A sensor with a threshold ``r`` times the finest one tells the
        controller about the room ``r`` times less often, and each report
        moves the estimate by ``r`` times as much. The weight grows with
        ``r²`` so the valve answers such a report as calmly as a fine sensor's.
        """
        return (self.threshold_K / MIN_REPORT_THRESHOLD_K) ** 2

    def observe(self, reading: float) -> float | None:
        """Record ``reading`` and return its band half-width if it is a repeat.

        Returns ``None`` for a fresh reading: the first one, or one that
        differs from the last. A repeat returns :attr:`threshold_K`.
        """
        last = self.last_reading
        self.last_reading = reading
        if last is None:
            return None
        if reading == last:
            return self.threshold_K
        self._steps.append(abs(reading - last))
        return None

    def forget_reading(self) -> None:
        """Drop the last reading, keeping the steps.

        The next reading then counts as fresh and adds no step: across a gap
        in the readings, or a reading from another source, its distance from
        the last one says nothing about the sensor.
        """
        self.last_reading = None

    def restore(self, last_reading: float | None, steps: Iterable[float]) -> None:
        """Adopt a persisted last reading and step history.

        Steps that are not positive carry no information about the threshold
        and are dropped.
        """
        self.last_reading = last_reading
        self._steps.clear()
        self._steps.extend(step for step in steps if step > 0.0)
