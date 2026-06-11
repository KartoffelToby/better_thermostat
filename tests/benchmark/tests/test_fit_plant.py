"""Plant-fit helper tests and CLI smoke."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile

from tests.benchmark.plant_fit import fit_plant, generate_synthetic_data


def _ts(
    values: list[float], start: float = 0.0, step_s: float = 3600.0
) -> fit_plant.TimeSeries:
    return fit_plant.TimeSeries(
        ts=[start + i * step_s for i in range(len(values))], val=values
    )


def test_align_drops_unmatched_samples():
    """Align drops unmatched samples."""
    a = _ts([1.0, 2.0, 3.0, 4.0])
    b = _ts([0.0, 1.0, 2.0])  # b is shorter
    aligned = fit_plant._align(a, b)
    assert len(aligned) == 3
    # Each entry: (t, a, b)
    assert aligned[0][1] == 1.0
    assert aligned[0][2] == 0.0


def test_find_cooling_windows_returns_monotonic_drops():
    """Find cooling windows returns monotonic drops."""
    # Build a synthetic series: cool from 22 → 19.6, then jump back up so
    # the next sample breaks the monotonic streak.
    room = [(float(i * 3600), 22.0 - 0.6 * i, 5.0) for i in range(5)]
    room.append((5 * 3600.0, 25.0, 5.0))  # sharp rise → closes the window
    windows = fit_plant._find_cooling_windows(room, min_drop_K=0.3, min_window_h=3)
    assert windows, "expected at least one cooling window"
    main = windows[0]
    assert len(main) == 5
    # Monotonically decreasing.
    vals = [v for _, v, _ in main]
    assert all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1))


def test_find_cooling_windows_rejects_short_or_shallow():
    """Find cooling windows rejects short or shallow."""
    # 2-hour drop is below the default min_window_h=3.
    room = [(0.0, 22.0, 5.0), (3600.0, 21.0, 5.0), (7200.0, 23.0, 5.0)]
    windows = fit_plant._find_cooling_windows(room, min_drop_K=0.3, min_window_h=3)
    assert windows == []


def test_fit_tau_returns_none_when_no_gradient():
    """Fit tau returns none when no gradient."""
    # T_out ≈ T0 → no driving gradient.
    window = [(0.0, 20.0, 19.9), (3600.0, 20.0, 19.9), (7200.0, 20.0, 19.9)]
    assert fit_plant._fit_tau_room(window) is None


def test_fit_tau_returns_none_for_too_few_samples():
    """Fit tau returns none for too few samples."""
    assert fit_plant._fit_tau_room([(0.0, 20.0, 5.0), (3600.0, 19.0, 5.0)]) is None


def test_fit_tau_returns_positive_finite_value_on_clean_decay():
    """Fit tau returns positive finite value on clean decay."""
    # Synthesise a clean exponential decay with tau = 600 min toward T_out=5.
    import math

    T_out = 5.0
    T0 = 22.0
    tau_true_min = 600.0
    window = []
    for i in range(20):
        t_s = i * 1800.0  # 30 min cadence
        t_min = t_s / 60.0
        T = T_out + (T0 - T_out) * math.exp(-t_min / tau_true_min)
        window.append((t_s, T, T_out))
    tau = fit_plant._fit_tau_room(window)
    assert tau is not None
    # 10 % tolerance.
    assert abs(tau - tau_true_min) / tau_true_min < 0.1


def test_find_heating_windows_returns_rising_windows():
    """Find heating windows returns rising windows."""
    room = [(float(i * 3600), 19.0 + 0.5 * i, 5.0) for i in range(4)]
    # Sharp drop to break the streak after the 4-entry rise.
    room.append((4 * 3600.0, 15.0, 5.0))
    windows = fit_plant._find_heating_windows(room, min_rise_K=0.3, min_window_h=2)
    assert windows, "expected at least one heating window"
    assert len(windows[0]) == 4


def test_max_heating_rate_returns_max_observed_slope():
    """Max heating rate returns max observed slope."""
    window = [
        (0.0, 19.0, 5.0),
        (3600.0, 19.5, 5.0),  # +0.5 K / 60 min = 0.00833 K/min
        (7200.0, 21.0, 5.0),  # +1.5 K / 60 min = 0.025 K/min  ← max
        (10800.0, 21.2, 5.0),
    ]
    rate = fit_plant._max_heating_rate(window)
    assert abs(rate - 0.025) < 1e-6


def test_max_heating_rate_empty_window_returns_zero():
    """Max heating rate empty window returns zero."""
    assert fit_plant._max_heating_rate([(0.0, 20.0, 5.0)]) == 0.0


def test_summarise_handles_empty():
    """Summarise handles empty."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fit_plant._summarise("label", [])
    assert "no usable windows" in buf.getvalue()


def test_summarise_prints_stats():
    """Summarise prints stats."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fit_plant._summarise("label", [10.0, 20.0, 30.0])
    out = buf.getvalue()
    assert "n=3" in out
    assert "mean=20.0" in out
    assert "median=20.0" in out


def test_main_in_memory_demo_runs():
    """Main in memory demo runs."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fit_plant.main()
    assert rc == 0
    out = buf.getvalue()
    assert "Outdoor proxy" in out
    assert "Room:" in out


def test_main_csv_path_runs_end_to_end():
    """Main csv path runs end to end."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "demo.csv"
        generate_synthetic_data.main(out_path=str(path), days=20)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fit_plant.main(str(path))
    assert rc == 0
    out = buf.getvalue()
    assert "Outdoor proxy" in out
    assert "Room:" in out


def test_main_missing_outdoor_proxy_returns_1():
    """Main missing outdoor proxy returns 1."""
    # Write a CSV that lacks any outdoor entity.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "demo.csv"
        with path.open("w") as f:
            f.write("2026-01-01 00:00:00,20.0,synthetic.living_room_temperature\n")
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = fit_plant.main(str(path))
        assert rc == 1
        assert "outdoor" in buf_err.getvalue()


def test_main_csv_discovers_arbitrary_entity_ids():
    """CSV mode maps non-synthetic entity ids to rooms + outdoor proxy."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.csv"
        with path.open("w") as f:
            for hour in range(24):
                t = f"2026-01-01 {hour:02d}:00:00"
                f.write(f"{t},{20.0 - hour * 0.05},sensor.bedroom_temp\n")
                f.write(f"{t},{2.0},sensor.outdoor_north\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fit_plant.main(str(path))
    assert rc == 0
    out = buf.getvalue()
    assert "sensor.outdoor_north" in out
    assert "sensor.bedroom_temp" in out


def test_summarise_uses_true_median_for_even_counts():
    """Even-length inputs report the mean of the two middle values."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fit_plant._summarise("x", [1.0, 2.0, 10.0, 11.0])
    assert "median=6.0" in buf.getvalue()
