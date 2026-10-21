"""Better Thermostat's room-temperature input stage on simulated time.

Production does not hand a sensor reading to the controllers as it
arrives. ``events/temperature.py`` commits a reading only when it moves
the committed value by at least 0.11 K, when same-direction moves add up
to that, when a smaller move holds for ``PLATEAU_ACCEPT_WINDOW`` seconds,
or when it is the first reading after an outage. The controllers see the
committed value. A time-based EMA (tau 300 s) follows the committed value
on every commit and on a one-minute tick, and the tick derives the
temperature slope from the EMA. PID and MPC v1 read the filtered value
and the slope as well.

The ladder's fallback onto the TRV's own reading during an outage is not
modelled: the stage holds the last committed value until the sensor
reports again.
"""

from __future__ import annotations

import math

from custom_components.better_thermostat.core.fsm.control_mode import LadderParams
from custom_components.better_thermostat.events.temperature import PLATEAU_ACCEPT_WINDOW

# Thresholds of ``trigger_temperature_change``.
SIGNIFICANCE_K = 0.11
MIN_COMMIT_INTERVAL_S = 5.0
# ``room_temperature_ema_tau_seconds`` and the period of the EMA tick.
EMA_TAU_S = 300.0
EMA_TICK_S = 60.0
# An outage that outlasts the ladder's downgrade debounce leaves OPTIMAL;
# the reading that ends it restarts the filter.
_OUTAGE_RESETS_FILTER_S = LadderParams().down_debounce_seconds


class RoomTemperatureInput:
    """Gate, filter and slope of the room temperature, as production keeps them."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget every reading, as a freshly started entity does."""
        self.room_temperature: float | None = None
        self.room_temperature_filtered: float | None = None
        self.temperature_slope_K_per_min: float | None = None
        self._ema: float | None = None
        self._ema_t: float | None = None
        self._slope_t: float | None = None
        self._next_tick_t: float | None = None
        self._last_commit_t: float | None = None
        self._accum_delta = 0.0
        self._accum_dir = 0
        self._pending: float | None = None
        self._pending_since: float | None = None
        self._commit_pending_at: float | None = None
        self._report: float | None = None
        self._outage_since: float | None = None
        self.commits = 0

    def update(self, t: float, reading: float | None) -> None:
        """Advance to ``t`` with the sensor's current report.

        Parameters
        ----------
        t : float
            Simulated time in seconds.
        reading : float or None
            The value the sensor reports at ``t``; ``None`` while it is
            unavailable. Only a report that differs from the previous one
            is an event, as a Home Assistant state change is.
        """
        self._run_timers(t)
        if reading is None:
            if self._outage_since is None and self._report is not None:
                self._outage_since = t
            self._report = None
            return
        rounded = round(reading, 2)
        if rounded != self._report:
            self._report = rounded
            self._on_reading(t, rounded)
        if self._next_tick_t is None:
            self._next_tick_t = t + EMA_TICK_S

    def _on_reading(self, t: float, value: float) -> None:
        outage_since = self._outage_since
        self._outage_since = None
        current = self.room_temperature
        if current is None:
            self._commit(t, value)
            return
        if outage_since is not None:
            if t - outage_since >= _OUTAGE_RESETS_FILTER_S:
                self._ema = None
                self._ema_t = None
            self._commit(t, value)
            return

        age = math.inf if self._last_commit_t is None else t - self._last_commit_t
        interval_ok = age > MIN_COMMIT_INTERVAL_S
        significant = round(abs(value - current), 2) >= SIGNIFICANCE_K

        signed_delta = round(value - current, 2)
        if signed_delta != 0:
            direction = 1 if signed_delta > 0 else -1
            if self._accum_dir in (0, direction):
                self._accum_delta = round(self._accum_delta + signed_delta, 2)
                self._accum_dir = direction
            else:
                self._accum_delta = signed_delta
                self._accum_dir = direction
            if self._pending != value:
                self._pending = value
                self._pending_since = t
                self._commit_pending_at = None
        else:
            self._accum_delta = 0.0
            self._accum_dir = 0
            self._pending = None
            self._pending_since = None
            self._commit_pending_at = None

        accumulated = abs(self._accum_delta) >= SIGNIFICANCE_K and interval_ok
        if (significant and interval_ok) or accumulated or self._plateau_due(t):
            self._commit(t, value)
        elif (
            not interval_ok
            and self._pending is not None
            and (significant or abs(self._accum_delta) >= SIGNIFICANCE_K)
        ):
            self._commit_pending_at = t + max(0.1, MIN_COMMIT_INTERVAL_S - age)

    def _plateau_due(self, t: float) -> bool:
        if self._pending is None or self._pending_since is None:
            return False
        if self._pending == self.room_temperature:
            return False
        if t - self._pending_since < PLATEAU_ACCEPT_WINDOW:
            return False
        last = self._last_commit_t
        return last is None or t - last > MIN_COMMIT_INTERVAL_S

    def _run_timers(self, t: float) -> None:
        pending = self._pending
        if pending is not None and pending == self._report:
            due = self._commit_pending_at
            if (due is not None and t >= due) or self._plateau_due(t):
                self._commit(t, pending)
        if self._next_tick_t is not None and t >= self._next_tick_t:
            self._tick(t)
            while self._next_tick_t <= t:
                self._next_tick_t += EMA_TICK_S

    def _tick(self, t: float) -> None:
        if self.room_temperature is None:
            return
        old_ema = self._ema
        old_t = self._slope_t
        new_ema = self._filter(t, self.room_temperature)
        if old_ema is not None and old_t is not None:
            elapsed_min = (t - old_t) / 60.0
            if elapsed_min > 0.1:
                self.temperature_slope_K_per_min = (new_ema - old_ema) / elapsed_min
        self._slope_t = t

    def _commit(self, t: float, value: float) -> None:
        self.commits += 1
        self.room_temperature = value
        self._filter(t, value)
        self._last_commit_t = t
        self._accum_delta = 0.0
        self._accum_dir = 0
        self._pending = None
        self._pending_since = None
        self._commit_pending_at = None

    def _filter(self, t: float, value: float) -> float:
        if self._ema is None or self._ema_t is None:
            ema = value
        else:
            dt = max(0.0, t - self._ema_t)
            alpha = 1.0 - math.exp(-dt / EMA_TAU_S) if dt > 0 else 0.0
            ema = self._ema + alpha * (value - self._ema)
        self._ema = ema
        self._ema_t = t
        self.room_temperature_filtered = round(ema, 2)
        return ema
