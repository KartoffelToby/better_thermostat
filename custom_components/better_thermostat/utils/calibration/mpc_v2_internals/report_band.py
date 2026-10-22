"""Reporting threshold of the room sensor, learned from the readings.

A room sensor reports when its value has moved by a threshold since the last
report: 0.1 K for a fine sensor, 0.5 K or more for coarse ones, plus a
periodic heartbeat. Between reports the controller sees the same value again.
The threshold says how far the room may have moved since; Better Thermostat
does not know it, so this tracker estimates it as the median step between
consecutive distinct readings. The median ignores the smaller steps a
heartbeat reports and the larger ones of an opened window, once there are
enough steps for those to be the minority.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
import statistics

# The bounds of the threshold estimate, in K. Below the lower one a sensor
# reports finer than the room's own noise; above the upper one a step is a
# room event, not the sensor's resolution.
MIN_REPORT_THRESHOLD_K = 0.1
MAX_REPORT_THRESHOLD_K = 1.0

# Number of recent steps the median runs over.
REPORT_STEP_WINDOW = 20

# Steps needed before the median counts. A single step may be an opened
# window or the switch to the TRVs' readings when the room sensor fails; with
# three, one such step still leaves the median on the sensor's own.
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
        median = statistics.median(self._steps)
        return max(MIN_REPORT_THRESHOLD_K, min(MAX_REPORT_THRESHOLD_K, median))

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

    def restore(self, last_reading: float | None, steps: Iterable[float]) -> None:
        """Adopt a persisted last reading and step history.

        Steps that are not positive carry no information about the threshold
        and are dropped.
        """
        self.last_reading = last_reading
        self._steps.clear()
        self._steps.extend(step for step in steps if step > 0.0)
