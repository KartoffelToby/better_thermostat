"""Synthetic weather generator unit tests."""

from __future__ import annotations

from tests.benchmark.weather.synthetic import (
    CHICAGO_LIKE,
    DENVER_LIKE,
    ClimateParams,
    _ar1_path,
    _daylight_hours,
    _seasonal_mean,
    _solar_peak,
    make_schedules,
)


def test_ar1_path_is_deterministic_for_given_seed():
    """Ar1 path is deterministic for given seed."""
    import random

    rng1 = random.Random(7)
    rng2 = random.Random(7)
    p1 = _ar1_path(n=100, alpha=0.9, sigma=2.0, mean=0.0, rng=rng1)
    p2 = _ar1_path(n=100, alpha=0.9, sigma=2.0, mean=0.0, rng=rng2)
    assert p1 == p2
    assert len(p1) == 100


def test_ar1_path_approximates_target_statistics():
    """Ar1 path approximates target statistics."""
    import random

    rng = random.Random(0)
    n = 20000
    path = _ar1_path(n=n, alpha=0.85, sigma=3.0, mean=5.0, rng=rng)
    mean = sum(path) / n
    var = sum((x - mean) ** 2 for x in path) / n
    # 5 % tolerance — generous, but enough to catch a regression.
    assert abs(mean - 5.0) < 0.2
    assert abs(var - 9.0) < 1.5  # sigma² = 9


def test_daylight_hours_within_bracketed_range():
    """Daylight hours within bracketed range."""
    p = CHICAGO_LIKE
    # Day 14 → winter min, day 14+183 → summer max
    winter = _daylight_hours(p.seasonal_min_day, p)
    summer = _daylight_hours(p.seasonal_min_day + 183, p)
    assert abs(winter - p.daylight_winter_h) < 0.5
    assert abs(summer - p.daylight_summer_h) < 0.5
    # Equinox is roughly in the middle.
    mid = _daylight_hours(p.seasonal_min_day + 91, p)
    assert p.daylight_winter_h < mid < p.daylight_summer_h


def test_solar_peak_winter_minimum():
    """Solar peak winter minimum."""
    p = CHICAGO_LIKE
    w = _solar_peak(p.seasonal_min_day, p)
    s = _solar_peak(p.seasonal_min_day + 183, p)
    assert abs(w - p.solar_peak_winter_W_m2) < 1.0
    assert abs(s - p.solar_peak_summer_W_m2) < 1.0


def test_seasonal_mean_extremes():
    """Seasonal mean extremes."""
    p = CHICAGO_LIKE
    cold = _seasonal_mean(p.seasonal_min_day, p)
    warm = _seasonal_mean(p.seasonal_min_day + 183, p)
    # Cold end is annual_mean − annual_amp, warm end is annual_mean + annual_amp.
    assert abs(cold - (p.annual_mean_C - p.annual_amp_C)) < 0.5
    assert abs(warm - (p.annual_mean_C + p.annual_amp_C)) < 0.5


def test_make_schedules_returns_pure_functions_of_time():
    """Make schedules returns pure functions of time."""
    outdoor, solar = make_schedules(CHICAGO_LIKE, duration_h=72, seed=1)
    assert outdoor(0.0) == outdoor(0.0)  # repeated call same result
    # Hour-by-hour outdoor values in a sensible winter range.
    for h in range(72):
        T = outdoor(h * 3600.0)
        assert -30.0 < T < 30.0, f"outdoor at h={h} out of range: {T}"
        s = solar(h * 3600.0)
        assert 0.0 <= s <= 1.0, f"solar at h={h} out of [0,1]: {s}"


def test_make_schedules_deterministic_for_seed():
    """Make schedules deterministic for seed."""
    o1, s1 = make_schedules(DENVER_LIKE, duration_h=24, seed=42)
    o2, s2 = make_schedules(DENVER_LIKE, duration_h=24, seed=42)
    samples1 = [(o1(t * 3600.0), s1(t * 3600.0)) for t in range(24)]
    samples2 = [(o2(t * 3600.0), s2(t * 3600.0)) for t in range(24)]
    assert samples1 == samples2


def test_solar_zero_outside_daylight():
    """Solar zero outside daylight."""
    _outdoor, solar = make_schedules(CHICAGO_LIKE, duration_h=48, seed=0)
    # Midnight: definitely no sun.
    assert solar(0.0 * 3600.0) == 0.0
    assert solar(2.0 * 3600.0) == 0.0
    # Noon: positive (cloud-modulated, so we only check >0 in principle).
    noon = solar(12.0 * 3600.0)
    assert 0.0 <= noon <= 1.0


def test_climate_preset_overrides_carry():
    """Climate preset overrides carry."""
    assert CHICAGO_LIKE.name == "cold_humid_continental"
    assert DENVER_LIKE.name == "cold_semi_arid"
    # Denver has lower cloud_mean than Chicago.
    assert DENVER_LIKE.cloud_mean < CHICAGO_LIKE.cloud_mean


def test_default_climate_params_constructible():
    """Default climate params constructible."""
    p = ClimateParams()
    assert p.annual_mean_C > 0
    assert p.synoptic_alpha < 1.0


def test_diurnal_cycle_peaks_mid_afternoon():
    """With synoptics muted, 15:00 must be warmer than the small hours."""
    calm = ClimateParams(name="calm", synoptic_sigma_C=0.0)
    outdoor, _solar = make_schedules(calm, start_day_of_year=14, duration_h=24, seed=1)
    assert outdoor(15 * 3600.0) > outdoor(3 * 3600.0)
