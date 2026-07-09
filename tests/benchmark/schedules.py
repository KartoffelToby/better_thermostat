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
    """Build a single-step schedule.

    Parameters
    ----------
    t_threshold_s : float
        Time of the step in seconds.
    before : float
        Value returned for ``t < t_threshold_s``.
    after : float
        Value returned for ``t >= t_threshold_s``.

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> value``.
    """
    return lambda t: after if t >= t_threshold_s else before


def constant(value: float) -> Callable[[float], float]:
    """Build a time-independent constant schedule.

    Parameters
    ----------
    value : float
        Value returned for every ``t``.

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> value``.
    """
    return lambda _t: value


def pulse(
    t_start_s: float, t_end_s: float, value: float, default: float = 0.0
) -> Callable[[float], float]:
    """Build a rectangular pulse schedule.

    Parameters
    ----------
    t_start_s : float
        Pulse start in seconds (inclusive).
    t_end_s : float
        Pulse end in seconds (exclusive).
    value : float
        Value returned inside ``[t_start_s, t_end_s)``.
    default : float
        Value returned outside the pulse.

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> value``.
    """
    return lambda t: value if t_start_s <= t < t_end_s else default


def pulse_bool(t_start_s: float, t_end_s: float) -> Callable[[float], bool]:
    """Build a boolean pulse schedule.

    Parameters
    ----------
    t_start_s : float
        Pulse start in seconds (inclusive).
    t_end_s : float
        Pulse end in seconds (exclusive).

    Returns
    -------
    Callable[[float], bool]
        Schedule function returning True inside ``[t_start_s, t_end_s)``.
    """
    return lambda t: t_start_s <= t < t_end_s


def ramp(
    t_start_s: float,
    t_end_s: float,
    start_value: float,
    end_value: float,
    after: float | None = None,
) -> Callable[[float], float]:
    """Build a piecewise linear ramp schedule.

    Constant ``start_value`` before ``t_start_s``, linear ramp to
    ``end_value``, constant ``after`` (or ``end_value`` if unspecified)
    afterwards.

    Parameters
    ----------
    t_start_s : float
        Ramp start in seconds. Must be strictly less than ``t_end_s``.
    t_end_s : float
        Ramp end in seconds.
    start_value : float
        Value before and at the start of the ramp.
    end_value : float
        Value at the end of the ramp.
    after : float or None
        Value after the ramp; defaults to ``end_value``.

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> value``.

    Raises
    ------
    ValueError
        If ``t_end_s <= t_start_s``.
    """
    if t_end_s <= t_start_s:
        raise ValueError("t_end_s must be greater than t_start_s")

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
    """Build a multi-step schedule from threshold/value pairs.

    Parameters
    ----------
    pairs : list of tuple of float
        ``(t_threshold_s, value)`` pairs; sorted internally.
    initial : float
        Value before the first threshold.

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> value``.
    """
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
    """Build a sinusoidal diurnal schedule.

    Minimum at simulation hour ``phase_min_h``; maximum ``period_h/2``
    later. Used for BOPTEST/Sinergym-style outdoor diurnal cycles.

    Parameters
    ----------
    min_value : float
        Minimum of the sinusoid.
    max_value : float
        Maximum of the sinusoid.
    period_h : float
        Period in hours.
    phase_min_h : float
        Simulation hour at which the minimum occurs.

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> value``.

    Raises
    ------
    ValueError
        If ``period_h`` is not positive (the cosine argument divides by it).
    """
    if period_h <= 0.0:
        raise ValueError(f"sinus_diurnal period_h must be > 0, got {period_h}")
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
    """Build a deterministic stochastic window-open schedule.

    ``count`` non-overlapping events distributed roughly evenly over
    ``duration_s`` — each event stays inside its own ``duration_s/count``
    slot. Reproducible for a given ``seed`` (IEA Annex 79 residential
    window-opening pattern, simplified).

    Parameters
    ----------
    seed : int
        RNG seed; identical seeds produce identical schedules.
    count : int
        Number of window-open events. Must be positive.
    duration_s : float
        Total schedule horizon in seconds.
    min_duration_s : float
        Lower bound of one event's duration in seconds.
    max_duration_s : float
        Upper bound of one event's duration in seconds.

    Returns
    -------
    Callable[[float], bool]
        Schedule function returning True while a window is open.

    Raises
    ------
    ValueError
        If ``count`` is not positive or the duration bounds are
        inconsistent.
    """
    if count <= 0:
        raise ValueError("count must be greater than 0")
    if duration_s <= 0.0:
        raise ValueError("duration_s must be greater than 0")
    if min_duration_s < 0.0 or max_duration_s < min_duration_s:
        raise ValueError(
            "duration bounds must satisfy 0 <= min_duration_s <= max_duration_s"
        )

    rng = random.Random(seed)
    slot_s = duration_s / count
    events: list[tuple[float, float]] = []
    for i in range(count):
        latest_start = slot_s - max_duration_s
        if latest_start <= 0.0:
            t_start = i * slot_s
        else:
            t_start = i * slot_s + rng.uniform(0.0, latest_start)
        # Clamp the duration to the remaining slot budget so short slots
        # cannot spill an event into the next slot.
        slot_remaining = (i + 1) * slot_s - t_start
        max_dur = min(max_duration_s, slot_remaining)
        min_dur = min(min_duration_s, max_dur)
        dur = rng.uniform(min_dur, max_dur)
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
    """Build a trapezoidal solar schedule: ramp up, plateau, ramp down.

    Parameters
    ----------
    t_rise_start : float
        Start of the rising edge in seconds.
    t_rise_end : float
        End of the rising edge / start of the plateau in seconds.
    t_fall_start : float
        End of the plateau / start of the falling edge in seconds.
    t_fall_end : float
        End of the falling edge in seconds.
    peak : float
        Plateau intensity (0.0 - 1.0).

    Returns
    -------
    Callable[[float], float]
        Schedule function ``t -> intensity``.

    Raises
    ------
    ValueError
        If the edges are not strictly ordered
        ``t_rise_start < t_rise_end <= t_fall_start < t_fall_end`` (a
        zero-width rising or falling edge would divide by zero).
    """
    if not (t_rise_start < t_rise_end <= t_fall_start < t_fall_end):
        raise ValueError(
            "solar_trapezoid requires t_rise_start < t_rise_end <= "
            "t_fall_start < t_fall_end, got "
            f"({t_rise_start}, {t_rise_end}, {t_fall_start}, {t_fall_end})"
        )

    def _f(t: float) -> float:
        if t < t_rise_start or t >= t_fall_end:
            return 0.0
        if t < t_rise_end:
            return peak * (t - t_rise_start) / (t_rise_end - t_rise_start)
        if t < t_fall_start:
            return peak
        return peak * (t_fall_end - t) / (t_fall_end - t_fall_start)

    return _f
