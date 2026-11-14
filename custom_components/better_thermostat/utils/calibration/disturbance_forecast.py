"""Assemble the learned disturbance profile and the weather forecast.

The shell calls this once per MPC v2 cycle. The era keeps Home Assistant out:
the input is a normalised tuple of hourly cloud factors and the output is the
plain ``predicted_disturbance_K_per_min`` tuple the optimiser consumes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math

from .disturbance_profile import DisturbanceProfile

# Clear-sky factor of a weather condition when the entity reports no cloud
# coverage. The value is the share of the clear-sky irradiance the condition
# lets through, not a cloud fraction.
_CONDITION_FACTORS: dict[str, float] = {
    "sunny": 1.0,
    "clear-night": 0.0,
    "partlycloudy": 0.6,
    "cloudy": 0.25,
    "fog": 0.1,
    "hail": 0.1,
    "lightning": 0.1,
    "lightning-rainy": 0.1,
    "pouring": 0.1,
    "rainy": 0.1,
    "snowy": 0.15,
    "snowy-rainy": 0.15,
    "windy": 0.5,
    "windy-variant": 0.5,
    "exceptional": 0.1,
}


@dataclass(frozen=True)
class HourlyForecastPoint:
    """One hourly weather forecast step, normalised for the profile."""

    timestamp: float
    clear_sky_factor: float


def clear_sky_factor(condition: object, cloud_coverage: object = None) -> float:
    """Return the share of clear-sky irradiance a forecast condition admits.

    ``cloud_coverage`` is Home Assistant's percent, which wins over the
    condition when present because it is the direct measurement. Unknown
    conditions and absurd coverages fall back to a neutral half sky rather
    than guessing sun or darkness.
    """
    if isinstance(cloud_coverage, int | float) and math.isfinite(float(cloud_coverage)):
        return max(0.0, min(1.0, 1.0 - float(cloud_coverage) / 100.0))
    if isinstance(condition, str):
        return _CONDITION_FACTORS.get(condition.lower(), 0.4)
    return 0.4


def _point_at(points: Sequence[HourlyForecastPoint], moment: float) -> float:
    """Return the clear-sky factor of the forecast step covering ``moment``.

    The latest point at or before the moment covers it; before the series the
    first point is used. An empty series is dark.
    """
    if not points:
        return 0.0
    covering = points[0]
    for point in points:
        if point.timestamp > moment:
            break
        covering = point
    return covering.clear_sky_factor


def clear_sky_at(points: Sequence[HourlyForecastPoint], moment: float) -> float:
    """Return the forecast clear-sky share covering ``moment``."""
    return _point_at(points, moment)


def build_predicted_disturbance(
    profile: DisturbanceProfile,
    hourly: Sequence[HourlyForecastPoint],
    *,
    now: datetime,
    steps: int,
    step_seconds: float,
) -> tuple[float, ...]:
    """Return the learned gain expected at each future MPC step.

    The profile predicts each step from its own wall-clock bucket, so a plan
    crossing midnight or a weekend boundary changes track at the right step.
    The forecast only scales the solar component; without a usable forecast
    the routine envelope remains.
    """
    if steps <= 0 or now.tzinfo is None or not math.isfinite(step_seconds):
        return ()
    values: list[float] = []
    for index in range(steps):
        wall = now + timedelta(seconds=index * step_seconds)
        predicted = profile.predict(
            weekday=wall.weekday(),
            month=wall.month,
            hour=wall.hour,
            minute=wall.minute,
            steps=1,
            step_minutes=step_seconds / 60.0,
            clear_sky_factors=(_point_at(hourly, wall.timestamp()),),
        )
        value = predicted[0] if predicted else 0.0
        values.append(value if math.isfinite(value) and value > 0.0 else 0.0)
    return tuple(values)
