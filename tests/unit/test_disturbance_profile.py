"""Unit tests for the learned disturbance profile."""

from __future__ import annotations

import math
from typing import Any

import pytest

from custom_components.better_thermostat.utils.calibration.disturbance_profile import (
    DAY_CATEGORIES,
    EMA_MIN_ALPHA,
    HALF_HOURS_PER_DAY,
    ROUTINE_BUCKETS,
    SEASONS,
    SOLAR_BUCKETS,
    DisturbanceProfile,
    day_category,
    half_hour,
    season,
    split_observation,
)


def test_clock_helpers_pick_the_expected_buckets() -> None:
    """Weekday, weekend, season and half-hour bins follow the documented ranges."""
    assert day_category(0) == 0
    assert day_category(4) == 0
    assert day_category(5) == 1
    assert day_category(6) == 1

    assert season(12) == season(1) == season(2) == 0
    assert season(3) == season(4) == season(5) == 1
    assert season(6) == season(7) == season(8) == 2
    assert season(9) == season(10) == season(11) == 3

    assert half_hour(0, 0) == 0
    assert half_hour(0, 29) == 0
    assert half_hour(0, 30) == 1
    assert half_hour(23, 59) == HALF_HOURS_PER_DAY - 1


def test_a_night_reading_is_routine_gain() -> None:
    """Without sun, the whole positive disturbance belongs to routine sources."""
    routine, solar = split_observation(0.02, clear_sky_factor=0.0, is_day=False)
    assert routine == pytest.approx(0.02)
    assert solar == 0.0


def test_a_sunny_reading_is_split_by_the_clear_sky_factor() -> None:
    """A clear sky attributes the whole gain to the sun, an overcast one less."""
    routine, solar = split_observation(0.03, clear_sky_factor=1.0, is_day=True)
    assert routine == pytest.approx(0.0)
    assert solar == pytest.approx(0.03)

    routine, solar = split_observation(0.03, clear_sky_factor=0.25, is_day=True)
    assert routine == pytest.approx(0.0225)
    assert solar == pytest.approx(0.0075)


def test_negative_disturbance_teaches_neither_profile() -> None:
    """A loss is already in the plant model and must not become a learned gain."""
    assert split_observation(-0.01, clear_sky_factor=1.0, is_day=True) == (0.0, 0.0)
    assert split_observation(float("nan"), 1.0, True) == (0.0, 0.0)


def test_the_first_sample_replaces_and_later_samples_average() -> None:
    """The bucket starts at the first observation and then follows the EMA."""
    profile = DisturbanceProfile()
    params = {
        "now_ts": 100.0,
        "weekday": 0,
        "month": 1,
        "hour": 12,
        "minute": 0,
        "is_day": False,
    }

    first = profile.learn(
        disturbance_kelvin_per_min=0.04, clear_sky_factor=0.0, **params
    )
    index = day_category(0) * HALF_HOURS_PER_DAY + half_hour(12, 0)
    assert first.routine_gain_kelvin_per_min[index] == pytest.approx(0.04)
    assert first.routine_samples[index] == pytest.approx(1.0)

    second = first.learn(disturbance_kelvin_per_min=0.0, clear_sky_factor=0.0, **params)
    assert second.routine_gain_kelvin_per_min[index] == pytest.approx(0.02)
    assert second.routine_samples[index] == pytest.approx(2.0)
    assert second.last_learned_ts == 100.0


def test_a_long_unseen_bucket_still_moves_at_the_minimum_rate() -> None:
    """A bucket with a large sample count still follows a new reading at the minimum rate."""
    profile = DisturbanceProfile()
    index = half_hour(8, 0)
    values = list(profile.routine_gain_kelvin_per_min)
    counts = list(profile.routine_samples)
    values[index] = 1.0
    counts[index] = 10_000.0
    profile = DisturbanceProfile(
        routine_gain_kelvin_per_min=tuple(values), routine_samples=tuple(counts)
    )

    learned = profile.learn(
        now_ts=1.0,
        weekday=0,
        month=1,
        hour=8,
        minute=0,
        disturbance_kelvin_per_min=0.0,
        clear_sky_factor=0.0,
        is_day=False,
    )

    assert learned.routine_gain_kelvin_per_min[index] == pytest.approx(
        1.0 - EMA_MIN_ALPHA
    )


def test_prediction_adds_the_routine_envelope_and_the_forecast_sun() -> None:
    """A forecast sun scales only the solar bucket, missing buckets stay dark."""
    profile = DisturbanceProfile()
    routine_index = day_category(2) * HALF_HOURS_PER_DAY + half_hour(9, 0)
    solar_index = season(6) * HALF_HOURS_PER_DAY + half_hour(9, 0)
    routine = list(profile.routine_gain_kelvin_per_min)
    solar = list(profile.solar_gain_kelvin_per_min)
    routine[routine_index] = 0.002
    routine[routine_index + 1] = 0.003
    solar[solar_index] = 0.01
    profile = DisturbanceProfile(
        routine_gain_kelvin_per_min=tuple(routine),
        solar_gain_kelvin_per_min=tuple(solar),
    )

    predicted = profile.predict(
        weekday=2,
        month=6,
        hour=9,
        minute=0,
        steps=2,
        step_minutes=30.0,
        clear_sky_factors=(1.0,),
    )

    assert predicted[0] == pytest.approx(0.012)
    assert predicted[1] == pytest.approx(0.003)


def test_a_profile_round_trips_through_the_stored_mapping() -> None:
    """The stored shape rebuilds the same buckets."""
    profile = DisturbanceProfile().learn(
        now_ts=42.0,
        weekday=6,
        month=7,
        hour=15,
        minute=30,
        disturbance_kelvin_per_min=0.02,
        clear_sky_factor=1.0,
        is_day=True,
    )

    rebuilt = DisturbanceProfile.from_mapping(profile.to_mapping(), key="room")

    assert rebuilt == profile
    assert len(rebuilt.routine_gain_kelvin_per_min) == ROUTINE_BUCKETS
    assert len(rebuilt.solar_gain_kelvin_per_min) == SOLAR_BUCKETS


def test_a_non_finite_bucket_falls_back_to_zero() -> None:
    """One poisoned bucket does not discard the rest of the profile."""
    stored: dict[str, Any] = DisturbanceProfile().to_mapping()
    stored["routine_gain_kelvin_per_min"][0] = math.nan
    stored["solar_gain_kelvin_per_min"][5] = math.inf

    rebuilt = DisturbanceProfile.from_mapping(stored, key="room")

    assert rebuilt.routine_gain_kelvin_per_min[0] == 0.0
    assert rebuilt.solar_gain_kelvin_per_min[5] == 0.0


def test_a_negative_stored_sample_count_restores_as_zero() -> None:
    """A count below zero would move a bucket further than its sample."""
    stored: dict[str, Any] = DisturbanceProfile().to_mapping()
    stored["routine_samples"][3] = -0.5
    stored["solar_samples"][7] = -4.0

    rebuilt = DisturbanceProfile.from_mapping(stored, key="room")

    assert rebuilt.routine_samples[3] == 0.0
    assert rebuilt.solar_samples[7] == 0.0


@pytest.mark.parametrize("broken", [None, {}, {"routine_gain_kelvin_per_min": [1.0]}])
def test_an_unusable_stored_profile_starts_from_zero(broken: object) -> None:
    """A stored section of the wrong shape restores the empty profile."""
    rebuilt = DisturbanceProfile.from_mapping(
        broken if isinstance(broken, dict) else {}, key="room"
    )

    assert rebuilt == DisturbanceProfile()
    assert (
        len(rebuilt.routine_gain_kelvin_per_min) == DAY_CATEGORIES * HALF_HOURS_PER_DAY
    )
    assert len(rebuilt.solar_gain_kelvin_per_min) == SEASONS * HALF_HOURS_PER_DAY
