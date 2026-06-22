"""Synthetic outdoor-temperature and solar generator.

Weather-driven scenarios expose controllers to *synoptic variability*
(multi-day weather fronts) layered on a *diurnal cycle* and a slow
*seasonal mean*. A short statistical model produces exactly that,
fully reproducible from a seed and parameter set, with no external
data dependency.

The model:

* Seasonal mean: cosine over the year, minimum at day 14 (mid-January).
* Diurnal cycle: cosine over the day, maximum at 15:00 and minimum
  12 hours earlier.
* Synoptic anomaly: AR(1) process on a 1-hour grid; lag-1
  autocorrelation around 0.9 (~24 h decorrelation timescale, matching
  observed mid-latitude winter synoptics).
* Solar: astronomical clear-sky envelope (parabolic between sunrise
  and sunset, scaled by season) × ``(1 − cloud_fraction)``. Cloud
  fraction is itself an AR(1) process clipped to ``[0, 1]``.

Two climate presets ship with the module — humid-continental and
semi-arid — bracketing the dynamics weather-driven scenarios probe.
Other presets can be added by copying the dataclass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import random


@dataclass(frozen=True)
class ClimateParams:
    """Statistical descriptors of a winter-week weather pattern.

    All temperatures in °C, all times in hours, irradiance in W/m².
    Defaults give a Chicago-like cold-humid-continental winter; the
    factory presets below override fields that differ.
    """

    name: str = "cold_humid_continental"
    # Climatological mean and seasonal swing (year-cycle amplitude).
    annual_mean_C: float = 9.0
    annual_amp_C: float = 13.0
    # Day-of-year at which the seasonal mean reaches its minimum.
    seasonal_min_day: int = 14
    # Diurnal swing (amplitude of the day-night cycle).
    diurnal_amp_C: float = 5.0
    # AR(1) synoptic anomaly: lag-1 autocorrelation and standard deviation
    # at steady state. ``alpha`` = e^(−1/tau_h) where tau_h is the
    # autocorrelation timescale in hours.
    synoptic_alpha: float = 0.92
    synoptic_sigma_C: float = 3.5
    # Solar: clear-sky peak irradiance (W/m²) at the winter solstice and
    # peak day length in hours. Both scale linearly with the cosine of
    # the season — summer peaks higher and lasts longer.
    solar_peak_winter_W_m2: float = 350.0
    solar_peak_summer_W_m2: float = 1000.0
    daylight_winter_h: float = 9.0
    daylight_summer_h: float = 15.0
    # Cloud fraction AR(1): mean, std, autocorrelation.
    cloud_mean: float = 0.5
    cloud_sigma: float = 0.3
    cloud_alpha: float = 0.85


CHICAGO_LIKE = ClimateParams(
    name="cold_humid_continental",
    annual_mean_C=9.0,
    annual_amp_C=13.0,
    diurnal_amp_C=5.0,
    synoptic_alpha=0.92,
    synoptic_sigma_C=3.5,
    cloud_mean=0.55,  # more overcast on average
    cloud_sigma=0.30,
)

DENVER_LIKE = ClimateParams(
    name="cold_semi_arid",
    annual_mean_C=10.0,
    annual_amp_C=12.0,
    diurnal_amp_C=8.0,  # high-altitude → sharper diurnal swing
    synoptic_alpha=0.94,  # weather fronts less frequent
    synoptic_sigma_C=4.5,  # but stronger when they arrive
    solar_peak_winter_W_m2=450.0,  # less cloud → more incoming solar
    cloud_mean=0.30,
    cloud_sigma=0.25,
)


# --- AR(1) sample generator (one path, hourly) ------------------------------


def _ar1_path(
    n: int, alpha: float, sigma: float, mean: float, rng: random.Random
) -> list[float]:
    """Generate one AR(1) path of length ``n`` with the requested moments.

    ``x[k] = mean + alpha · (x[k-1] − mean) + ε`` where ``ε`` is white
    noise sized so the stationary variance matches ``sigma²``.

    Raises
    ------
    ValueError
        If ``n < 1``, ``|alpha| >= 1`` (non-stationary; the innovation
        variance would go negative), or ``sigma < 0``.
    """
    if n < 1:
        raise ValueError(f"_ar1_path n must be >= 1, got {n}")
    if not -1.0 < alpha < 1.0:
        raise ValueError(
            f"_ar1_path requires |alpha| < 1 for a stationary process, got {alpha}"
        )
    if sigma < 0.0:
        raise ValueError(f"_ar1_path sigma must be >= 0, got {sigma}")
    # Drive the AR(1) at its stationary variance.
    inno_sigma = sigma * math.sqrt(1.0 - alpha * alpha)
    x = mean + rng.gauss(0.0, sigma)
    out: list[float] = [x]
    for _ in range(1, n):
        x = mean + alpha * (x - mean) + rng.gauss(0.0, inno_sigma)
        out.append(x)
    return out


# --- Astronomical helpers ---------------------------------------------------


def _daylight_hours(day_of_year: int, params: ClimateParams) -> float:
    """Length of day in hours, linearly interpolated between solstices."""
    # Cosine with min at day 14 (winter solstice analogue, matching seasonal_min_day).
    cycle = math.cos(2.0 * math.pi * (day_of_year - params.seasonal_min_day) / 365.0)
    mid = (params.daylight_summer_h + params.daylight_winter_h) / 2.0
    amp = (params.daylight_summer_h - params.daylight_winter_h) / 2.0
    return mid - amp * cycle


def _solar_peak(day_of_year: int, params: ClimateParams) -> float:
    """Clear-sky peak irradiance for the day (W/m²)."""
    cycle = math.cos(2.0 * math.pi * (day_of_year - params.seasonal_min_day) / 365.0)
    mid = (params.solar_peak_summer_W_m2 + params.solar_peak_winter_W_m2) / 2.0
    amp = (params.solar_peak_summer_W_m2 - params.solar_peak_winter_W_m2) / 2.0
    return mid - amp * cycle


def _seasonal_mean(day_of_year: int, params: ClimateParams) -> float:
    return params.annual_mean_C - params.annual_amp_C * math.cos(
        2.0 * math.pi * (day_of_year - params.seasonal_min_day) / 365.0
    )


# --- Public builders --------------------------------------------------------


def make_schedules(
    params: ClimateParams,
    start_day_of_year: int = 14,
    duration_h: int = 168,
    seed: int = 0,
) -> tuple[Callable[[float], float], Callable[[float], float]]:
    """Return ``(outdoor_schedule, solar_schedule)`` for the requested slice.

    The schedules are pure functions of ``t`` (seconds) but reference
    pre-generated AR(1) paths internally — they are deterministic for a
    given ``(params, start_day_of_year, duration_h, seed)`` triple.
    """
    rng = random.Random(seed)
    # Hourly grid plus one wrap entry so linear interpolation between
    # samples is well-defined at the end of the slice.
    n_hours = duration_h + 1

    synoptic = _ar1_path(
        n_hours,
        alpha=params.synoptic_alpha,
        sigma=params.synoptic_sigma_C,
        mean=0.0,
        rng=rng,
    )
    cloud_raw = _ar1_path(
        n_hours,
        alpha=params.cloud_alpha,
        sigma=params.cloud_sigma,
        mean=params.cloud_mean,
        rng=rng,
    )
    cloud = [max(0.0, min(1.0, c)) for c in cloud_raw]

    def outdoor(t_s: float) -> float:
        hours_in = t_s / 3600.0
        i = int(hours_in)
        frac = hours_in - i
        day = start_day_of_year + hours_in / 24.0
        hour_of_day = (hours_in + 0.0) % 24.0
        seasonal = _seasonal_mean(int(day), params)
        diurnal = params.diurnal_amp_C * math.cos(
            2.0 * math.pi * (hour_of_day - 15.0) / 24.0
        )
        # Linear interp the synoptic anomaly between hourly samples.
        # Clamp both ends so a negative t (or t past the path) cannot wrap
        # around via negative indexing.
        s0 = synoptic[max(0, min(i, len(synoptic) - 1))]
        s1 = synoptic[max(0, min(i + 1, len(synoptic) - 1))]
        return seasonal + diurnal + s0 + frac * (s1 - s0)

    def solar(t_s: float) -> float:
        hours_in = t_s / 3600.0
        i = int(hours_in)
        frac = hours_in - i
        day = int(start_day_of_year + hours_in / 24.0)
        hour_of_day = hours_in % 24.0
        daylen = _daylight_hours(day, params)
        sunrise = 12.0 - daylen / 2.0
        sunset = 12.0 + daylen / 2.0
        if hour_of_day < sunrise or hour_of_day > sunset:
            return 0.0
        # Parabolic clear-sky envelope, 0 at sunrise/sunset, peak at noon.
        x = (hour_of_day - sunrise) / (sunset - sunrise)
        envelope = 4.0 * x * (1.0 - x)
        peak = _solar_peak(day, params)
        # Linear interp the cloud fraction between hourly samples.
        # Clamp both ends so a negative t cannot wrap via negative indexing.
        c0 = cloud[max(0, min(i, len(cloud) - 1))]
        c1 = cloud[max(0, min(i + 1, len(cloud) - 1))]
        clouds = c0 + frac * (c1 - c0)
        w_per_m2 = peak * envelope * (1.0 - clouds)
        # Normalise to [0..1] against a 1000 W/m² clear-sky reference.
        return max(0.0, min(1.0, w_per_m2 / 1000.0))

    return outdoor, solar
