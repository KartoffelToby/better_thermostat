"""Fit a Two-State RC plant to recorder-style temperature data.

Reads a long-format CSV (``timestamp,value,entity_id``) matching what
Home Assistant's ``recorder`` ``statistics`` table exports, and
estimates ``tau_room_min`` for each configured room. The companion
script :mod:`generate_synthetic_data` produces a fully-synthetic CSV
with documented ground-truth ``PlantParams`` so the methodology can be
exercised end-to-end without leaking real-world sensor IDs.

Approach:

1. Pivot the long-format CSV into per-entity time series.
2. Find natural cooling phases — windows of N consecutive hours where
   the room temperature is monotonically decreasing. Fit an exponential
   decay ``T(t) = T_outdoor + (T0 - T_outdoor) * exp(-t / tau)`` to each
   window to estimate ``tau_room_min``.
3. Find natural heating phases — windows where the room temperature is
   monotonically increasing. The slope (K/min) gives an upper bound on
   effective heating rate for the as-installed system; with a known
   ``T_water``, this yields a coarse ``gain_heater`` estimate.

Output: print mean / median estimates per room.

Note on accuracy: fitting from hourly samples conflates the room
time-constant with the radiator's thermal-mass transient in the first
~30 min after a valve close. For fast envelopes (``tau_room < 10 h``)
this biases the fit upward. The included synthetic dataset shows the
order-of-magnitude consistency rather than exact recovery — see the
companion module for ground-truth values.
"""

from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass
import math
import statistics
import sys


@dataclass
class TimeSeries:
    """Parallel arrays of timestamp + value."""

    ts: list[float]
    val: list[float]


def _load_csv(path: str) -> dict[str, TimeSeries]:
    """Load long-format CSV (time, value, entity_id) into per-entity series."""
    from datetime import datetime

    per_entity: dict[str, TimeSeries] = defaultdict(lambda: TimeSeries([], []))
    with open(path) as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 3:
                continue
            t_str, val_str, eid = row[0], row[1], row[2]
            try:
                ts = datetime.fromisoformat(t_str).timestamp()
                val = float(val_str)
            except ValueError, TypeError:
                continue
            if not math.isfinite(val):
                continue
            per_entity[eid].ts.append(ts)
            per_entity[eid].val.append(val)
    return dict(per_entity)


def _align(
    series_a: TimeSeries, series_b: TimeSeries
) -> list[tuple[float, float, float]]:
    """Align two series by timestamp (assumed to be the same hourly grid)."""
    b_map = {round(t, 0): v for t, v in zip(series_b.ts, series_b.val)}
    out: list[tuple[float, float, float]] = []
    for t, a in zip(series_a.ts, series_a.val):
        if round(t, 0) in b_map:
            out.append((t, a, b_map[round(t, 0)]))
    return out


def _find_cooling_windows(
    room: list[tuple[float, float, float]],
    min_drop_K: float = 0.3,
    min_window_h: int = 3,
) -> list[list[tuple[float, float, float]]]:
    """Return contiguous windows of monotonically-decreasing room temp."""
    windows: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
    for i in range(len(room)):
        if not cur:
            cur.append(room[i])
            continue
        if room[i][1] <= cur[-1][1]:
            cur.append(room[i])
        else:
            if len(cur) >= min_window_h and (cur[0][1] - cur[-1][1]) >= min_drop_K:
                windows.append(cur)
            cur = [room[i]]
    if len(cur) >= min_window_h and (cur[0][1] - cur[-1][1]) >= min_drop_K:
        windows.append(cur)
    return windows


def _fit_tau_room(window: list[tuple[float, float, float]]) -> float | None:
    """Fit exponential decay T(t) = T_out + (T0 - T_out) exp(-t/tau) for one window."""
    if len(window) < 3:
        return None
    t0_s, T0, _ = window[0]
    # Use the window's *median* outdoor temp during the cooling phase.
    T_out = sum(w[2] for w in window) / len(window)
    if abs(T0 - T_out) < 0.5:
        return None  # No driving gradient, can't fit

    # Linearise: ln((T - T_out) / (T0 - T_out)) = -t / tau
    xs = []
    ys = []
    for t_s, T, _ in window:
        dT = T - T_out
        dT0 = T0 - T_out
        if dT * dT0 <= 0:
            continue  # Sign flip, skip
        ratio = dT / dT0
        if ratio <= 0:
            continue
        xs.append((t_s - t0_s) / 60.0)  # minutes
        ys.append(math.log(ratio))
    if len(xs) < 3:
        return None
    # Linear least squares: y = slope * x + 0  →  slope = -1/tau
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    den = sum((xs[i] - mean_x) ** 2 for i in range(n))
    if den == 0:
        return None
    slope = num / den
    if slope >= 0:
        return None  # Not actually cooling
    tau = -1.0 / slope
    return tau


def _find_heating_windows(
    room: list[tuple[float, float, float]],
    min_rise_K: float = 0.3,
    min_window_h: int = 2,
) -> list[list[tuple[float, float, float]]]:
    """Return windows of monotonically-rising room temp."""
    windows: list[list[tuple[float, float, float]]] = []
    cur: list[tuple[float, float, float]] = []
    for i in range(len(room)):
        if not cur:
            cur.append(room[i])
            continue
        if room[i][1] >= cur[-1][1]:
            cur.append(room[i])
        else:
            if len(cur) >= min_window_h and (cur[-1][1] - cur[0][1]) >= min_rise_K:
                windows.append(cur)
            cur = [room[i]]
    if len(cur) >= min_window_h and (cur[-1][1] - cur[0][1]) >= min_rise_K:
        windows.append(cur)
    return windows


def _max_heating_rate(window: list[tuple[float, float, float]]) -> float:
    """Return the maximum K/min rise observed in the window."""
    rates = []
    for i in range(1, len(window)):
        dt_min = (window[i][0] - window[i - 1][0]) / 60.0
        if dt_min <= 0:
            continue
        rates.append((window[i][1] - window[i - 1][1]) / dt_min)
    return max(rates) if rates else 0.0


def _summarise(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: no usable windows")
        return
    values_sorted = sorted(values)
    n = len(values_sorted)
    median = statistics.median(values_sorted)
    mean = sum(values_sorted) / n
    print(
        f"  {label}: n={n}, mean={mean:.1f}, median={median:.1f}, "
        f"min={values_sorted[0]:.1f}, max={values_sorted[-1]:.1f}"
    )


def main(path: str | None = None) -> int:
    """Fit RC plant parameters from temperature recordings.

    ``path`` is the long-format CSV exported by Home Assistant's
    ``recorder`` ``statistics`` table — the natural input for users who
    bring their own data. Entity IDs are discovered from the CSV itself:
    the first entity containing ``outdoor`` (case-insensitive) becomes
    the outdoor proxy, every other entity is fitted as a room. When
    ``None`` (default), the demo uses the deterministic synthetic dataset
    from :mod:`generate_synthetic_data` directly, in memory, with no file
    round-trip.
    """
    if path is None:
        from . import generate_synthetic_data

        print("(demo) loading synthetic dataset in memory…")
        data = generate_synthetic_data.generate_dataset()
        outdoor_id = generate_synthetic_data.OUTDOOR_ENTITY_ID
        rooms = {
            spec.name: spec.entity_id for spec in generate_synthetic_data.DEFAULT_ROOMS
        }
    else:
        try:
            data = _load_csv(path)
        except OSError as err:
            print(f"ERROR: could not read {path}: {err}", file=sys.stderr)
            return 1
        outdoor_candidates = sorted(eid for eid in data if "outdoor" in eid.lower())
        if not outdoor_candidates:
            print(
                "ERROR: no entity with 'outdoor' in its id found in CSV",
                file=sys.stderr,
            )
            return 1
        outdoor_id = outdoor_candidates[0]
        rooms = {
            eid.rsplit(".", 1)[-1]: eid for eid in sorted(data) if eid != outdoor_id
        }
    if outdoor_id not in data:
        print(f"ERROR: outdoor proxy {outdoor_id} not in CSV", file=sys.stderr)
        return 1

    outdoor = data[outdoor_id]
    print(f"Outdoor proxy: {outdoor_id} ({len(outdoor.ts)} samples)")
    print(
        f"  range {min(outdoor.val):.1f} .. {max(outdoor.val):.1f} °C, "
        f"mean {sum(outdoor.val) / len(outdoor.val):.1f} °C"
    )

    for room_name, sensor_id in rooms.items():
        if sensor_id not in data:
            print(f"\nROOM {room_name}: sensor {sensor_id} missing")
            continue

        print(f"\n=== Room: {room_name} ({sensor_id}) ===")
        aligned = _align(data[sensor_id], outdoor)
        print(f"  aligned samples: {len(aligned)}")
        if len(aligned) < 10:
            continue

        cooling_windows = _find_cooling_windows(aligned)
        print(f"  cooling windows (≥3 h, drop ≥0.3 K): {len(cooling_windows)}")
        taus = []
        for w in cooling_windows:
            tau = _fit_tau_room(w)
            if tau is not None and 30.0 <= tau <= 6000.0:
                taus.append(tau)
        _summarise("tau_room_min (cooling-based)", taus)

        heating_windows = _find_heating_windows(aligned)
        print(f"  heating windows (≥2 h, rise ≥0.3 K): {len(heating_windows)}")
        rates = [_max_heating_rate(w) for w in heating_windows]
        rates = [r for r in rates if r > 0]
        _summarise("max heating rate (K/min)", rates)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
