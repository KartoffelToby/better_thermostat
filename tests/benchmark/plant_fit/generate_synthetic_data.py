"""Generate a synthetic HA-style dataset for the plant-fitting demo.

The dataset that lives alongside :mod:`fit_plant` mimics the long-format
CSV that ``recorder``'s statistics table produces (one row per sample with
columns ``timestamp,value,entity_id``). This script produces such a file
end-to-end from a published plant model, so:

* The methodology is **reproducible** — anyone can regenerate the CSV.
* The ground-truth ``PlantParams`` are **documented** (constants below),
  giving ``fit_plant`` a known answer to recover.
* No real-world sensor entity_ids or recorded readings leak into the repo.

The synthesis runs ``PlantModelRC2`` for two rooms at 30 s plant-tick over
~60 days, driven by:

* a daily-cycle outdoor temperature with seasonal drift and small noise;
* a bang-bang thermostat per room (open valve below ``T_room < T_low``,
  close above ``T_room > T_high``) — produces the natural heating /
  cooling phases that ``fit_plant``'s window finder relies on.

The output is decimated to hourly samples (matching the original CSV's
cadence) so ``fit_plant.py`` runs against equivalent input.
"""

from __future__ import annotations

from collections.abc import Iterable
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from pathlib import Path
import random
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fit_plant import TimeSeries

# Make the benchmark package importable when running this script directly.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.benchmark.plant import PlantParams, PlantState, TwoStatePlant  # noqa: E402

# --- Ground-truth plant params for the two simulated rooms -------------------
# Numerically close to what the original real-data fit recovered, so the
# downstream PROFILE_FITTED_* constants in plant.py stay representative of
# typical mid-European apartment envelopes.

GROUND_TRUTH_LIVING = PlantParams(
    tau_room_min=570.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)

GROUND_TRUTH_KITCHEN = PlantParams(
    tau_room_min=1011.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)


# --- Outdoor weather pattern -------------------------------------------------


@dataclass(frozen=True)
class OutdoorParams:
    """Parameters of the synthetic outdoor-temperature signal."""

    base_C: float = 3.0  # seasonal mean over the 60-day window
    seasonal_amp_C: float = 4.0  # warmer at the start, colder mid-winter
    diurnal_amp_C: float = 4.0  # day/night swing
    noise_sigma_C: float = 0.4  # short-term variability


def _outdoor_at(t_min: float, params: OutdoorParams, rng: random.Random) -> float:
    """Return outdoor temperature at minute ``t_min`` since simulation start."""
    days = t_min / (60.0 * 24.0)
    # Seasonal: cold dip ~30 days in (peak of January).
    seasonal = -params.seasonal_amp_C * math.cos(2.0 * math.pi * (days - 30.0) / 60.0)
    # Diurnal: minimum at ~05:00 local, maximum at ~15:00.
    hour = (t_min / 60.0) % 24.0
    diurnal = -params.diurnal_amp_C * math.cos(2.0 * math.pi * (hour - 15.0) / 24.0)
    noise = rng.gauss(0.0, params.noise_sigma_C)
    return params.base_C + seasonal + diurnal + noise


# --- Day / night setback schedule and bang-bang thermostat ------------------


def _setpoints_at(
    t_min: float, T_day: float, T_night: float, band: float
) -> tuple[float, float]:
    """Return (T_low, T_high) for the bang-bang thermostat at minute ``t_min``.

    Day setpoint is active 07:00-22:00 local time; the rest of the day uses
    a lower setback. The night setback is what produces multi-hour
    monotonic-cooling windows long enough for the τ-fit to be well-posed.
    """
    hour = (t_min / 60.0) % 24.0
    sp = T_day if 7.0 <= hour < 22.0 else T_night
    return sp - 0.5 * band, sp + 0.5 * band


def _bang_bang(T_room: float, last_u: float, T_low: float, T_high: float) -> float:
    """Open valve below ``T_low``, close above ``T_high``; hold otherwise."""
    if T_room < T_low:
        return 1.0
    if T_room > T_high:
        return 0.0
    return last_u


# --- Simulation -------------------------------------------------------------


@dataclass(frozen=True)
class RoomSpec:
    """One simulated room — plant model, setback schedule, output entity_id."""

    name: str
    entity_id: str
    plant_params: PlantParams
    T_day_C: float = 21.0
    T_night_C: float = 17.0
    band_K: float = 0.5
    T_init: float = 20.0


def _simulate_room(
    spec: RoomSpec,
    outdoor: list[float],
    dt_s: float,
    rng: random.Random,
    sensor_noise_K: float = 0.05,
) -> list[float]:
    """Return per-tick simulated room temperatures (length == len(outdoor))."""
    initial = PlantState(T_room_C=spec.T_init, T_rad_C=spec.T_init)
    plant = TwoStatePlant(spec.plant_params, initial=initial)
    out: list[float] = []
    u = 0.0
    for i, T_out in enumerate(outdoor):
        t_min = i * dt_s / 60.0
        T_low, T_high = _setpoints_at(t_min, spec.T_day_C, spec.T_night_C, spec.band_K)
        u = _bang_bang(plant.state.T_room_C, u, T_low, T_high)
        plant.step(dt_s=dt_s, u=u, T_outdoor_C=T_out)
        noisy = plant.state.T_room_C + rng.gauss(0.0, sensor_noise_K)
        out.append(noisy)
    return out


# --- CSV emission -----------------------------------------------------------


def _emit_csv(
    path: Path,
    start: datetime,
    hourly_outdoor: list[float],
    rooms_hourly: dict[str, list[float]],
    outdoor_entity_id: str,
) -> None:
    """Write the long-format CSV consumed by ``fit_plant.py``."""
    rows: list[tuple[str, str, str]] = []
    for hour, T_out in enumerate(hourly_outdoor):
        ts = (start + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S")
        rows.append((ts, f"{T_out:.4f}", outdoor_entity_id))
    for entity_id, samples in rooms_hourly.items():
        for hour, T in enumerate(samples):
            ts = (start + timedelta(hours=hour)).strftime("%Y-%m-%d %H:%M:%S")
            rows.append((ts, f"{T:.4f}", entity_id))
    rows.sort(key=lambda r: (r[2], r[0]))  # match the original CSV ordering
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


OUTDOOR_ENTITY_ID = "synthetic.outdoor_temperature"
DEFAULT_ROOMS = (
    RoomSpec(
        name="living",
        entity_id="synthetic.living_room_temperature",
        plant_params=GROUND_TRUTH_LIVING,
    ),
    RoomSpec(
        name="kitchen",
        entity_id="synthetic.kitchen_temperature",
        plant_params=GROUND_TRUTH_KITCHEN,
    ),
)


# --- In-memory dataset ------------------------------------------------------


def generate_dataset(
    days: int = 60, seed: int = 20260527, start: datetime | None = None
) -> dict[str, TimeSeries]:
    """Generate the synthetic dataset as in-memory time series.

    Returns a ``dict[entity_id] -> TimeSeries`` with hourly samples for
    one outdoor proxy and the two simulated rooms. Deterministic for a
    given ``(days, seed, start)`` triple. Plant-fitting demos consume
    this directly without a CSV round-trip.
    """
    from .fit_plant import TimeSeries  # avoid module-load-time cycle

    rng = random.Random(seed)
    dt_s = 30.0
    n_ticks = int(days * 24 * 60 * 60 / dt_s)

    outdoor_params = OutdoorParams()
    outdoor = [
        _outdoor_at(i * dt_s / 60.0, outdoor_params, rng) for i in range(n_ticks)
    ]
    room_series: dict[str, list[float]] = {
        spec.entity_id: _simulate_room(spec, outdoor, dt_s, rng)
        for spec in DEFAULT_ROOMS
    }

    samples_per_hour = int(3600 / dt_s)

    def _hourly(series: Iterable[float]) -> list[float]:
        s = list(series)
        return [s[i] for i in range(0, len(s), samples_per_hour)]

    hourly_outdoor = _hourly(outdoor)
    rooms_hourly = {eid: _hourly(s) for eid, s in room_series.items()}

    epoch = start or datetime(2026, 1, 1, 0, 0, 0)
    timestamps = [
        (epoch + timedelta(hours=h)).timestamp() for h in range(len(hourly_outdoor))
    ]
    series: dict[str, TimeSeries] = {
        OUTDOOR_ENTITY_ID: TimeSeries(ts=list(timestamps), val=list(hourly_outdoor))
    }
    for eid, vals in rooms_hourly.items():
        series[eid] = TimeSeries(ts=list(timestamps), val=list(vals))
    return series


# --- CLI: optional CSV dump --------------------------------------------------


def main(
    out_path: str = "tests/benchmark/plant_fit/synthetic_dataset.csv",
    days: int = 60,
    seed: int = 20260527,
) -> int:
    """Generate the synthetic dataset and dump it to ``out_path`` as CSV.

    Primary use is *demonstrating the recorder-export format* — the
    benchmark's in-tree fit demo runs from :func:`generate_dataset`
    directly, no file round-trip needed.
    """
    start = datetime(2026, 1, 1, 0, 0, 0)
    series = generate_dataset(days=days, seed=seed, start=start)

    # Convert hourly TimeSeries back to per-entity float arrays for CSV.
    samples_per_series = next(iter(series.values()))
    n_hours = len(samples_per_series.ts)
    hourly_outdoor = series[OUTDOOR_ENTITY_ID].val
    rooms_hourly = {
        eid: ts.val for eid, ts in series.items() if eid != OUTDOOR_ENTITY_ID
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    _emit_csv(
        out, start, hourly_outdoor, rooms_hourly, outdoor_entity_id=OUTDOOR_ENTITY_ID
    )

    print(f"Wrote {out} — {n_hours} hourly samples × {len(series)} series.")
    print("Ground truth plant params:")
    for spec in DEFAULT_ROOMS:
        print(
            f"  {spec.name}: tau_room_min={spec.plant_params.tau_room_min:.0f}, "
            f"gain_heater={spec.plant_params.gain_heater}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
