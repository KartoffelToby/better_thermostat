"""Calibration offset at its range limit: tell a stuck offset from a busy one.

Offset calibration adds to the offset the TRV holds whatever gap remains
between the room temperature and the TRV's reading. A TRV that applies the
offset closes the gap, and the offset settles. A TRV that ignores it keeps
reporting the same reading, so every control cycle adds the same gap again
until the offset rests at the limit of the TRV's range. A TRV whose reading is
further from the room than its range reaches ends there too. In both cases the
correction the gap asks for lies past the limit, and offset calibration has
nothing left to correct with.

An offset also reaches a limit on a TRV that applies it: while a cold room
heats up, or because a calibration mode or overheating protection drives it
there on purpose. The correction the gap asks for then lies inside the range,
since the TRV's reading carries the offset. Only an offset whose correction
has stayed past the same limit, through every control cycle of
``CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS``, counts as stuck there.

The module decides and holds nothing; the caller keeps the run between
control cycles and reads the time.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Final

# Twelve five-minute control ticks. The ratchet of a TRV that ignores its
# offset reaches the limit within a few cycles and never leaves it again. A
# TRV that applies the offset reports a reading that follows it within one of
# its measurement intervals, up to a quarter of an hour on a sleepy Matter or
# Zigbee head, and the correction is back inside the range in the cycle after
# that. An hour gives that recovery several intervals of margin, and a room
# that is miscalibrated for an hour longer loses nothing a user would notice.
CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS: Final = 3600.0

# What a reported offset may be off its grid and still sit on the limit: the
# read-back grid of Home Assistant's number entities is 0.01.
_LIMIT_MATCH_TOLERANCE_KELVIN: Final = 0.01


@dataclass(frozen=True)
class OffsetAtLimit:
    """An unbroken run of control cycles with the offset stuck at one limit.

    Attributes
    ----------
    limit : float
        The limit of the TRV's offset range the offset sits at, in Kelvin.
    since_monotonic : float
        Monotonic time of the first cycle of the run, in seconds.
    """

    limit: float
    since_monotonic: float


def _limit_band(lower: float, upper: float, step: float) -> float:
    """Return how far inside a limit an offset still sits at it, in Kelvin.

    One step, as the offset is rounded onto the TRV's step before it is
    written, but no more than an eighth of the range, a quarter of the way
    from a limit to the middle of a symmetric one, so a narrow range does not
    read as a limit everywhere.
    """
    band = step if math.isfinite(step) and step > 0 else 0.0
    band = min(band, (upper - lower) / 8)
    return max(band, 0.0) + _LIMIT_MATCH_TOLERANCE_KELVIN


def limit_held_by(
    calibration_offset: float, lower: float, upper: float, step: float
) -> float | None:
    """Return the limit of the offset range that ``calibration_offset`` sits at.

    An offset within one step of a limit sits at it: the offset is rounded
    onto the TRV's step before it is written, so the last step below the
    upper limit is as far as some ranges let it go. A range narrower than
    eight steps counts an eighth of its width instead. An offset of zero, or
    one on the other side of zero from a limit, never sits at that limit.

    Parameters
    ----------
    calibration_offset : float
        The offset in Kelvin, in Better Thermostat's convention.
    lower : float
        The lowest offset the TRV accepts, in Better Thermostat's convention.
    upper : float
        The highest offset the TRV accepts, in Better Thermostat's convention.
    step : float
        The TRV's offset step in Kelvin; a step that is not a positive finite
        number counts as none.

    Returns
    -------
    float | None
        ``upper`` or ``lower`` when the offset sits at that limit, ``None``
        when it sits at neither or a bound is not finite.
    """
    if not (
        math.isfinite(calibration_offset)
        and math.isfinite(lower)
        and math.isfinite(upper)
    ):
        return None
    band = _limit_band(lower, upper, step)
    if calibration_offset > 0 and calibration_offset >= upper - band:
        return upper
    if calibration_offset < 0 and calibration_offset <= lower + band:
        return lower
    return None


def compute_offset_correction(
    room_temperature: float,
    trv_reading: float,
    calibration_offset: float,
    *,
    shifts_setpoint: bool,
) -> float:
    """Return the offset the gap between room and TRV reading asks for.

    This is the correction offset calibration starts from before any
    calibration mode, tolerance or protection adjusts it. A TRV that adds
    its offset to its reading reports that sum, so the correction is the gap
    plus the offset it holds. A TRV that adds its offset to its setpoint
    reports its bare reading and acts on the offset with the opposite sign,
    so its correction is the gap negated, whatever offset it holds.

    Parameters
    ----------
    room_temperature : float
        The room temperature in °C the calibration works from.
    trv_reading : float
        The temperature in °C the TRV reports.
    calibration_offset : float
        The offset in Kelvin the TRV holds, in Better Thermostat's convention.
    shifts_setpoint : bool
        Whether the TRV adds its offset to its setpoint instead of its reading.

    Returns
    -------
    float
        The offset in Kelvin, in Better Thermostat's convention.
    """
    if shifts_setpoint:
        return trv_reading - room_temperature
    return room_temperature - trv_reading + calibration_offset


def is_correction_past_limit(correction: float, limit: float, step: float) -> bool:
    """Return whether a correction lies more than one step past ``limit``.

    Parameters
    ----------
    correction : float
        The offset in Kelvin the gap asks for, from
        ``compute_offset_correction``.
    limit : float
        The limit of the range the offset sits at, in Kelvin.
    step : float
        The TRV's offset step in Kelvin; a step that is not a positive finite
        number counts as none.

    Returns
    -------
    bool
        True when the correction lies past the limit, on the side away from
        zero, by more than one step.
    """
    margin = step if math.isfinite(step) and step > 0 else 0.0
    margin += _LIMIT_MATCH_TOLERANCE_KELVIN
    if limit > 0:
        return correction > limit + margin
    if limit < 0:
        return correction < limit - margin
    return False


def advance_offset_at_limit(
    run: OffsetAtLimit | None, limit: float | None, now_monotonic: float
) -> OffsetAtLimit | None:
    """Return the run after one control cycle that found the offset at ``limit``.

    A cycle at the run's limit continues it; a cycle at the other limit
    starts a new run, and a cycle at neither ends it.

    Parameters
    ----------
    run : OffsetAtLimit | None
        The run before this cycle, ``None`` when there is none.
    limit : float | None
        The limit this cycle found the offset stuck at, ``None`` for neither.
    now_monotonic : float
        Monotonic time of this cycle, in seconds.

    Returns
    -------
    OffsetAtLimit | None
        The run after this cycle, ``None`` when there is none.
    """
    if limit is None:
        return None
    if run is not None and run.limit == limit:
        return run
    return OffsetAtLimit(limit=limit, since_monotonic=now_monotonic)


def is_offset_stuck_at_limit(
    run: OffsetAtLimit | None,
    now_monotonic: float,
    window_seconds: float = CALIBRATION_OFFSET_AT_LIMIT_WINDOW_SECONDS,
) -> bool:
    """Return whether the offset has stayed at its limit for the whole window.

    Parameters
    ----------
    run : OffsetAtLimit | None
        The current run, ``None`` when there is none.
    now_monotonic : float
        Monotonic time of this cycle, in seconds.
    window_seconds : float
        How long the run has to have lasted.

    Returns
    -------
    bool
        True once the run has lasted at least ``window_seconds``.
    """
    if run is None:
        return False
    return now_monotonic - run.since_monotonic >= window_seconds
