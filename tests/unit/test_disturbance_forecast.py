"""Tests for the weather-normalised disturbance forecast."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.better_thermostat.utils.calibration.disturbance_forecast import (
    HourlyForecastPoint,
    build_predicted_disturbance,
    clear_sky_at,
    clear_sky_factor,
)
from custom_components.better_thermostat.utils.calibration.disturbance_profile import (
    DisturbanceProfile,
)

MONDAY_NOON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def _point(hours_ahead: float, factor: float) -> HourlyForecastPoint:
    return HourlyForecastPoint(
        timestamp=(MONDAY_NOON + timedelta(hours=hours_ahead)).timestamp(),
        clear_sky_factor=factor,
    )


def test_condition_factors_rank_sun_over_cloud_and_rain() -> None:
    assert clear_sky_factor("sunny") == 1.0
    assert clear_sky_factor("partlycloudy") == 0.6
    assert clear_sky_factor("cloudy") == 0.25
    assert clear_sky_factor("rainy") == 0.1
    assert clear_sky_factor("clear-night") == 0.0


def test_cloud_coverage_wins_over_the_condition() -> None:
    assert clear_sky_factor("sunny", 25) == 0.75
    assert clear_sky_factor("sunny", 25.0) == 0.75
    assert clear_sky_factor("rainy", 0) == 1.0
    assert clear_sky_factor("sunny", 150) == 0.0


def test_unknown_condition_and_coverage_fall_back() -> None:
    assert clear_sky_factor("unknown") == 0.4
    assert clear_sky_factor(None) == 0.4
    assert clear_sky_factor("sunny", "25") == 1.0
    assert clear_sky_factor("sunny", float("nan")) == 1.0


def test_clear_sky_at_picks_the_covering_hour() -> None:
    points = (_point(0, 0.2), _point(1, 0.9), _point(2, 0.5))
    start = MONDAY_NOON.timestamp()
    assert clear_sky_at(points, start) == 0.2
    assert clear_sky_at(points, start + 3599) == 0.2
    assert clear_sky_at(points, start + 3600) == 0.9
    assert clear_sky_at(points, start - 3600) == 0.2
    assert clear_sky_at(points, start + 10_000) == 0.5
    assert clear_sky_at((), start) == 0.0


def test_the_builder_reads_each_step_from_its_own_bucket() -> None:
    profile = DisturbanceProfile().learn(
        now_ts=MONDAY_NOON.timestamp(),
        weekday=0,
        month=1,
        hour=12,
        minute=0,
        disturbance_kelvin_per_min=0.02,
        clear_sky_factor=0.5,
        is_day=True,
    )
    values = build_predicted_disturbance(
        profile, (_point(0, 0.5),), now=MONDAY_NOON, steps=3, step_seconds=1800.0
    )
    # Only the learned 12:00-12:29 bucket carries a prediction; the next
    # half-hour bucket is unlearned and stays zero rather than inventing a gain.
    assert values == (0.02, 0.0, 0.0)


def test_a_missing_forecast_leaves_only_the_routine_gain() -> None:
    profile = DisturbanceProfile().learn(
        now_ts=MONDAY_NOON.timestamp(),
        weekday=0,
        month=1,
        hour=12,
        minute=0,
        disturbance_kelvin_per_min=0.02,
        clear_sky_factor=0.5,
        is_day=True,
    )
    values = build_predicted_disturbance(
        profile, (), now=MONDAY_NOON, steps=1, step_seconds=1800.0
    )
    assert values == (0.01,)


def test_a_midnight_crossing_changes_the_routine_bucket() -> None:
    profile = (
        DisturbanceProfile()
        .learn(
            now_ts=MONDAY_NOON.timestamp(),
            weekday=0,
            month=1,
            hour=23,
            minute=30,
            disturbance_kelvin_per_min=0.01,
            clear_sky_factor=0.0,
            is_day=False,
        )
        .learn(
            now_ts=MONDAY_NOON.timestamp(),
            weekday=0,
            month=1,
            hour=0,
            minute=0,
            disturbance_kelvin_per_min=0.03,
            clear_sky_factor=0.0,
            is_day=False,
        )
    )
    before_midnight = datetime(2026, 1, 5, 23, 30, tzinfo=UTC)
    values = build_predicted_disturbance(
        profile, (), now=before_midnight, steps=2, step_seconds=1800.0
    )
    assert values[0] == 0.01
    assert values[1] == 0.03


def test_empty_or_invalid_horizon_returns_nothing() -> None:
    profile = DisturbanceProfile()
    assert (
        build_predicted_disturbance(
            profile, (), now=MONDAY_NOON, steps=0, step_seconds=1800.0
        )
        == ()
    )
    naive = datetime(2026, 1, 5, 12, 0)
    assert (
        build_predicted_disturbance(
            profile, (), now=naive, steps=2, step_seconds=1800.0
        )
        == ()
    )
    assert (
        build_predicted_disturbance(
            profile, (), now=MONDAY_NOON, steps=2, step_seconds=float("nan")
        )
        == ()
    )
