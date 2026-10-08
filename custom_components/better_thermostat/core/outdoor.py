"""Damped outdoor temperature and the summer-mode switch.

Whether a room needs heating depends on the outdoor temperature of the last
day or so, not on the current reading: a sunny afternoon does not warm a
building that cooled down overnight. Heating controllers therefore judge the
season from a damped outdoor temperature, a first-order low-pass filter over
the readings with a time constant of about a day.

Each reading counts for as long as it was current. A sensor reports on
change, so it sends many readings while the temperature moves and few while
it holds; weighing readings by count would let a warm, fast-moving afternoon
outweigh a long, steady night.

Summer mode switches on once the damped temperature reaches the threshold
and off only after it has dropped a hysteresis band below it, so a damped
temperature that hovers at the threshold does not toggle the TRVs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import math

# Time constant of the outdoor filter. After one time constant a step in the
# outdoor temperature has moved the damped value by 63 %, after two by 86 %.
OUTDOOR_DAMPING_TIME_CONSTANT = timedelta(hours=24)

# How far the damped temperature has to fall below the threshold before a
# room in summer mode heats again.
SUMMER_MODE_HYSTERESIS_KELVIN = 1.0


@dataclass(frozen=True, slots=True)
class DampedOutdoorTemperature:
    """The filter state after the latest reading.

    ``value`` is the damped temperature at ``reading_at``. The reading itself
    holds from ``reading_at`` until the next one arrives.
    """

    value: float
    reading: float
    reading_at: float


def start_damping(reading: float, reading_at: float) -> DampedOutdoorTemperature:
    """Start the filter at ``reading``, taken at ``reading_at`` (epoch seconds)."""
    return DampedOutdoorTemperature(
        value=reading, reading=reading, reading_at=reading_at
    )


def damped_value_at(
    state: DampedOutdoorTemperature,
    at: float,
    time_constant_seconds: float = OUTDOOR_DAMPING_TIME_CONSTANT.total_seconds(),
) -> float:
    """Return the damped temperature at ``at`` with the last reading held.

    A time at or before the last reading returns the value at that reading.
    """
    elapsed_seconds = at - state.reading_at
    if not elapsed_seconds > 0:
        return state.value
    weight = -math.expm1(-elapsed_seconds / time_constant_seconds)
    return state.value + weight * (state.reading - state.value)


def add_reading(
    state: DampedOutdoorTemperature | None,
    reading: float,
    reading_at: float,
    time_constant_seconds: float = OUTDOOR_DAMPING_TIME_CONSTANT.total_seconds(),
) -> DampedOutdoorTemperature | None:
    """Return the filter state after ``reading``, taken at ``reading_at``.

    The previous reading is held up to ``reading_at``. A non-finite reading
    and one that is not newer than the last reading leave the state as it
    is, so the same sensor state passed twice counts once.
    """
    if not (math.isfinite(reading) and math.isfinite(reading_at)):
        return state
    if state is None:
        return start_damping(reading, reading_at)
    if not reading_at > state.reading_at:
        return state
    return DampedOutdoorTemperature(
        value=damped_value_at(state, reading_at, time_constant_seconds),
        reading=reading,
        reading_at=reading_at,
    )


def heat_threshold(off_temperature: float, call_for_heat: bool) -> float:
    """Return the outdoor temperature below which the room heats.

    A heating room stops at ``off_temperature``; a room in summer mode
    resumes only below ``off_temperature`` minus the hysteresis band.
    """
    if call_for_heat:
        return off_temperature
    return off_temperature - SUMMER_MODE_HYSTERESIS_KELVIN
