"""Reusable setpoint / outdoor / disturbance schedule builders.

Every helper returns a function ``t -> value`` (or ``t -> bool``) that the
scenario runner samples at each simulation tick. Keeping them in their own
module lets ``scenarios.py`` stay a pure registry of ``ScenarioConfig``
literals.
"""

from __future__ import annotations

from collections.abc import Callable
import math
import random


def step(t_threshold_s: float, before: float, after: float) -> Callable[[float], float]:
    """``before`` until ``t_threshold_s``, ``after`` from then on."""
    return lambda t: after if t >= t_threshold_s else before


def constant(value: float) -> Callable[[float], float]:
    """Time-independent constant signal."""
    return lambda _t: value


def pulse(
    t_start_s: float, t_end_s: float, value: float, default: float = 0.0
) -> Callable[[float], float]:
    """Return ``value`` for t in [t_start, t_end) and ``default`` otherwise."""
    return lambda t: value if t_start_s <= t < t_end_s else default


def pulse_bool(t_start_s: float, t_end_s: float) -> Callable[[float], bool]:
    """Return True for t in [t_start, t_end), False otherwise."""
    return lambda t: t_start_s <= t < t_end_s


def ramp(
    t_start_s: float,
    t_end_s: float,
    start_value: float,
    end_value: float,
    after: float | None = None,
) -> Callable[[float], float]:
    """Piecewise linear ramp.

    Constant ``start_value`` before ``t_start``, linear ramp to
    ``end_value``, constant ``after`` (or ``end_value`` if unspecified)
    afterwards.
    """
    final = after if after is not None else end_value

    def _f(t: float) -> float:
        if t < t_start_s:
            return start_value
        if t >= t_end_s:
            return final
        frac = (t - t_start_s) / (t_end_s - t_start_s)
        return start_value + frac * (end_value - start_value)

    return _f


def piecewise_step(
    pairs: list[tuple[float, float]], initial: float
) -> Callable[[float], float]:
    """Step function defined by ``(t_threshold, value)`` pairs."""
    sorted_pairs = sorted(pairs)

    def _f(t: float) -> float:
        value = initial
        for t_thresh, v in sorted_pairs:
            if t >= t_thresh:
                value = v
            else:
                break
        return value

    return _f


def sinus_diurnal(
    min_value: float, max_value: float, period_h: float = 24.0, phase_min_h: float = 6.0
) -> Callable[[float], float]:
    """Sinusoidal diurnal profile.

    Minimum at simulation hour ``phase_min_h``; maximum ``period_h/2``
    later. Used for BOPTEST/Sinergym-style outdoor diurnal cycles.
    """
    period_s = period_h * 3600.0
    offset_s = phase_min_h * 3600.0
    amp = (max_value - min_value) / 2.0
    mid = (max_value + min_value) / 2.0
    return lambda t: mid - amp * math.cos(2.0 * math.pi * (t - offset_s) / period_s)


def stochastic_windows(
    seed: int,
    count: int,
    duration_s: float,
    min_duration_s: float = 5 * 60.0,
    max_duration_s: float = 30 * 60.0,
) -> Callable[[float], bool]:
    """Deterministic stochastic window-open schedule.

    ``count`` non-overlapping events distributed roughly evenly over
    ``duration_s``. Reproducible for a given ``seed`` (IEA Annex 79
    residential window-opening pattern, simplified).
    """
    rng = random.Random(seed)
    slot_s = duration_s / count
    events: list[tuple[float, float]] = []
    for i in range(count):
        latest_start = slot_s - max_duration_s
        if latest_start <= 0.0:
            t_start = i * slot_s
        else:
            t_start = i * slot_s + rng.uniform(0.0, latest_start)
        dur = rng.uniform(min_duration_s, max_duration_s)
        events.append((t_start, t_start + dur))

    def schedule(t: float) -> bool:
        return any(start <= t < end for start, end in events)

    return schedule


def solar_trapezoid(
    t_rise_start: float,
    t_rise_end: float,
    t_fall_start: float,
    t_fall_end: float,
    peak: float = 1.0,
) -> Callable[[float], float]:
    """Trapezoidal solar profile: ramp up, plateau, ramp down."""

    def _f(t: float) -> float:
        if t < t_rise_start or t >= t_fall_end:
            return 0.0
        if t < t_rise_end:
            return peak * (t - t_rise_start) / (t_rise_end - t_rise_start)
        if t < t_fall_start:
            return peak
        return peak * (t_fall_end - t) / (t_fall_end - t_fall_start)

    return _f
