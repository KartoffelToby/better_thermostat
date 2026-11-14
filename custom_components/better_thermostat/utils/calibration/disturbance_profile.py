"""Learned recurring exogenous gains for the MPC v2 look-ahead horizon.

The Kalman disturbance observer sees the sum of every heat gain and loss the
2R2C plant model does not explain. Two parts of that sum repeat: routine
internal gains (home office, cooking, occupancy) on a weekly schedule, and
solar gains on a seasonal schedule driven by the room's orientation and
shading. This module learns both from the observer's positive disturbance in
half-hour buckets and replays them ahead of the control cycle.

The learner is deliberately shell-side: it reads wall-clock timestamps from
its caller and never touches Home Assistant. The pure decision core receives
the resulting forecast as a tuple and stays deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
import math

_LOGGER = logging.getLogger(__name__)

# One bucket per half hour, one routine track per day category, one solar
# track per season. The layout keeps the stored payload small: a bucket is
# one float, not a record with its own timestamp.
HALF_HOURS_PER_DAY = 48
DAY_CATEGORIES = 2
SEASONS = 4
ROUTINE_BUCKETS = DAY_CATEGORIES * HALF_HOURS_PER_DAY
SOLAR_BUCKETS = SEASONS * HALF_HOURS_PER_DAY

# An EMA sample count starts at zero; the first sample replaces the bucket and
# later samples move it by at most this fraction. The floor keeps a bucket
# that was learned long ago able to follow a changed routine instead of
# freezing under an ever smaller gain.
EMA_MIN_ALPHA = 0.02

# A clear-sky normalised solar sample needs a minimum factor: dividing by a
# near-dark sky would turn sensor noise into a huge solar envelope.
MIN_SOLAR_FACTOR = 0.2


def day_category(weekday: int) -> int:
    """Return ``0`` for Monday–Friday and ``1`` for Saturday–Sunday."""
    return 1 if weekday >= 5 else 0


def season(month: int) -> int:
    """Return the season bin of a calendar month (``1``–``12``)."""
    if month in (12, 1, 2):
        return 0
    if month in (3, 4, 5):
        return 1
    if month in (6, 7, 8):
        return 2
    return 3


def half_hour(hour: int, minute: int) -> int:
    """Return the half-hour bucket of a wall-clock time."""
    return (hour * 60 + minute) // 30 % HALF_HOURS_PER_DAY


def split_observation(
    disturbance_kelvin_per_min: float, clear_sky_factor: float, is_day: bool
) -> tuple[float, float]:
    """Split one positive disturbance reading into routine and solar parts.

    Negative disturbance is a loss the plant model already carries, so it
    teaches neither profile. During the day the clear-sky factor decides how
    much of the gain is attributed to the sun; at night the whole positive
    gain belongs to routine sources.
    """
    if not math.isfinite(disturbance_kelvin_per_min):
        return 0.0, 0.0
    gain = max(0.0, disturbance_kelvin_per_min)
    if not is_day or not math.isfinite(clear_sky_factor):
        return gain, 0.0
    factor = max(0.0, min(1.0, clear_sky_factor))
    solar = gain * factor
    return gain - solar, solar


def _clamped_bucket(value: int, size: int) -> int:
    """Return *value* wrapped into ``[0, size)``."""
    return value % size


@dataclass(frozen=True)
class DisturbanceProfile:
    """Learned routine and solar gain envelopes of one room."""

    routine_gain_kelvin_per_min: tuple[float, ...] = (0.0,) * ROUTINE_BUCKETS
    solar_gain_kelvin_per_min: tuple[float, ...] = (0.0,) * SOLAR_BUCKETS
    routine_samples: tuple[float, ...] = (0.0,) * ROUTINE_BUCKETS
    solar_samples: tuple[float, ...] = (0.0,) * SOLAR_BUCKETS
    last_learned_ts: float | None = None

    def learn(
        self,
        *,
        now_ts: float,
        weekday: int,
        month: int,
        hour: int,
        minute: int,
        disturbance_kelvin_per_min: float,
        clear_sky_factor: float,
        is_day: bool,
    ) -> DisturbanceProfile:
        """Return the profile with one observer reading folded into its bucket.

        The update is a sample-count EMA: a bucket's first sample replaces its
        value and later samples move it by ``1/(n+1)``, floored at
        :data:`EMA_MIN_ALPHA`. The count is the bucket's age proxy — a bucket
        fed every day settles, one that has not been seen for weeks still
        follows a changed routine.
        """
        routine_obs, solar_obs = split_observation(
            disturbance_kelvin_per_min, clear_sky_factor, is_day
        )
        routine_index = day_category(weekday) * HALF_HOURS_PER_DAY + half_hour(
            hour, minute
        )
        solar_index = season(month) * HALF_HOURS_PER_DAY + half_hour(hour, minute)

        solar_target = 0.0
        if solar_obs > 0.0 and math.isfinite(clear_sky_factor):
            solar_target = solar_obs / max(MIN_SOLAR_FACTOR, min(1.0, clear_sky_factor))

        routine_values = list(self.routine_gain_kelvin_per_min)
        routine_counts = list(self.routine_samples)
        solar_values = list(self.solar_gain_kelvin_per_min)
        solar_counts = list(self.solar_samples)

        routine_values[routine_index], routine_counts[routine_index] = _ema(
            routine_values[routine_index], routine_counts[routine_index], routine_obs
        )
        solar_values[solar_index], solar_counts[solar_index] = _ema(
            solar_values[solar_index], solar_counts[solar_index], solar_target
        )
        return replace(
            self,
            routine_gain_kelvin_per_min=tuple(routine_values),
            routine_samples=tuple(routine_counts),
            solar_gain_kelvin_per_min=tuple(solar_values),
            solar_samples=tuple(solar_counts),
            last_learned_ts=now_ts,
        )

    def predict(
        self,
        *,
        weekday: int,
        month: int,
        hour: int,
        minute: int,
        steps: int,
        step_minutes: float,
        clear_sky_factors: tuple[float, ...] = (),
    ) -> tuple[float, ...]:
        """Return the predicted gain for each of the next *steps*.

        Times beyond the supplied clear-sky series are read as dark, which
        leaves only the routine envelope. A missing bucket predicts zero, so
        an unlearned profile never invents a gain.
        """
        if steps <= 0:
            return ()
        day = day_category(weekday)
        season_index = season(month)
        start_minutes = hour * 60 + minute
        values: list[float] = []
        for index in range(steps):
            minutes = start_minutes + index * step_minutes
            bucket = int(minutes // 30) % HALF_HOURS_PER_DAY
            routine = self.routine_gain_kelvin_per_min[
                day * HALF_HOURS_PER_DAY + bucket
            ]
            solar = self.solar_gain_kelvin_per_min[
                season_index * HALF_HOURS_PER_DAY + bucket
            ]
            factor = 0.0
            if index < len(clear_sky_factors):
                candidate = clear_sky_factors[index]
                if math.isfinite(candidate):
                    factor = max(0.0, min(1.0, candidate))
            value = routine + solar * factor
            values.append(value if math.isfinite(value) else 0.0)
        return tuple(values)

    def to_mapping(self) -> dict[str, object]:
        """Return the JSON-serialisable payload stored for this room."""
        return {
            "routine_gain_kelvin_per_min": list(self.routine_gain_kelvin_per_min),
            "solar_gain_kelvin_per_min": list(self.solar_gain_kelvin_per_min),
            "routine_samples": list(self.routine_samples),
            "solar_samples": list(self.solar_samples),
            "last_learned_ts": self.last_learned_ts,
        }

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object], *, key: str | None = None
    ) -> DisturbanceProfile:
        """Rebuild a profile from a stored payload, refusing poisoned buckets.

        A bucket list of the wrong shape replaces the whole profile with its
        zero default; a single non-finite entry falls back to the bucket's
        default of zero. Both are named in the log, so a corrupt store is
        visible rather than silently indistinguishable from a new room.
        """
        try:
            routine = _stored_buckets(
                raw.get("routine_gain_kelvin_per_min"), ROUTINE_BUCKETS, key, "routine"
            )
            solar = _stored_buckets(
                raw.get("solar_gain_kelvin_per_min"), SOLAR_BUCKETS, key, "solar"
            )
            routine_samples = _stored_counts(
                raw.get("routine_samples"), ROUTINE_BUCKETS, key, "routine samples"
            )
            solar_samples = _stored_counts(
                raw.get("solar_samples"), SOLAR_BUCKETS, key, "solar samples"
            )
            last_raw = raw.get("last_learned_ts")
            last_ts = (
                None
                if last_raw is None
                else float(last_raw)
                if isinstance(last_raw, int | float) and math.isfinite(float(last_raw))
                else None
            )
        except TypeError, ValueError, OverflowError:
            _LOGGER.warning(
                "better_thermostat: stored disturbance profile for %s is "
                "unusable; learning starts from zero",
                key or "an unnamed room",
            )
            return cls()
        return cls(
            routine_gain_kelvin_per_min=routine,
            solar_gain_kelvin_per_min=solar,
            routine_samples=routine_samples,
            solar_samples=solar_samples,
            last_learned_ts=last_ts,
        )


def _ema(value: float, count: float, sample: float) -> tuple[float, float]:
    """Return the next EMA value and its updated sample count."""
    alpha = max(EMA_MIN_ALPHA, 1.0 / (count + 1.0))
    return value + alpha * (sample - value), count + 1.0


def _stored_buckets(
    value: object, size: int, key: str | None, name: str
) -> tuple[float, ...]:
    """Return one bucket list of exactly *size* finite floats."""
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise TypeError(f"{name} is not a list of {size} entries")
    result: list[float] = []
    for item in value:
        number = float(item) if isinstance(item, int | float) else 0.0
        if not math.isfinite(number):
            _LOGGER.warning(
                "better_thermostat: stored disturbance profile for %s has a "
                "non-finite %s bucket; using zero",
                key or "an unnamed room",
                name,
            )
            number = 0.0
        result.append(number)
    return tuple(result)


def _stored_counts(
    value: object, size: int, key: str | None, name: str
) -> tuple[float, ...]:
    """Return one list of sample counts, a count below zero read as zero.

    A negative count would move a bucket further than its sample.
    """
    return tuple(max(0.0, count) for count in _stored_buckets(value, size, key, name))
