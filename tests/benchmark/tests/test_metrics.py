"""Tests for metric computations on synthetic time series."""

from __future__ import annotations

import math

import pytest

from tests.benchmark.metrics import TimeSeries, compute_metrics


def _flat_series(
    value: float, setpoint: float, n: int = 100, dt_s: float = 60.0
) -> TimeSeries:
    t = [i * dt_s for i in range(n)]
    return TimeSeries(
        t_s=t,
        T_room=[value] * n,
        T_setpoint=[setpoint] * n,
        valve_percent=[0.0] * n,
        heat_to_room_K_per_min=[0.0] * n,
    )


def test_zero_error_no_overshoot():
    """A constant series at setpoint yields zero error metrics."""
    series = _flat_series(20.0, 20.0)
    m = compute_metrics(series, transient_start_s=0.0)
    assert m.max_overshoot_K == 0.0
    assert m.max_undershoot_K == 0.0
    assert m.rmse_tracking_K == 0.0


def test_overshoot_detected():
    """A single positive spike is recorded as overshoot."""
    n = 100
    t = [i * 60.0 for i in range(n)]
    T_room = [20.0 + (0.5 if i == 50 else 0.0) for i in range(n)]
    series = TimeSeries(
        t_s=t,
        T_room=T_room,
        T_setpoint=[20.0] * n,
        valve_percent=[0.0] * n,
        heat_to_room_K_per_min=[0.0] * n,
    )
    m = compute_metrics(series, transient_start_s=0.0)
    assert abs(m.max_overshoot_K - 0.5) < 1e-9


def test_settling_time_detected():
    """Settling is reported once the band is held continuously for the dwell."""
    n = 50
    dt_s = 60.0
    t = [i * dt_s for i in range(n)]
    T_setpoint = [21.0] * n
    T_room = [20.0 if i < 5 else 21.0 for i in range(n)]
    series = TimeSeries(
        t_s=t,
        T_room=T_room,
        T_setpoint=T_setpoint,
        valve_percent=[0.0] * n,
        heat_to_room_K_per_min=[0.0] * n,
    )
    m = compute_metrics(series, transient_start_s=0.0)
    # Enters band at t=5min, dwell requirement is 10min, so settling
    # reported as "entered band at minute 5".
    assert m.settling_time_min == 5.0


def test_settling_inf_when_never_in_band():
    """If the band is never reached the settling time is +inf."""
    n = 100
    t = [i * 60.0 for i in range(n)]
    series = TimeSeries(
        t_s=t,
        T_room=[20.0] * n,
        T_setpoint=[22.0] * n,
        valve_percent=[0.0] * n,
        heat_to_room_K_per_min=[0.0] * n,
    )
    m = compute_metrics(series, transient_start_s=0.0)
    assert math.isinf(m.settling_time_min)


def test_valve_cycle_count():
    """Direction reversals in a synthetic valve trajectory are counted correctly."""
    n = 10
    series = TimeSeries(
        t_s=[i * 60.0 for i in range(n)],
        T_room=[20.0] * n,
        T_setpoint=[20.0] * n,
        # 0 -> 50 (up) -> 50 -> 0 (down) -> 0 -> 50 (up) -> 50 -> 0 (down)
        valve_percent=[0.0, 50.0, 50.0, 0.0, 0.0, 50.0, 50.0, 0.0, 0.0, 0.0],
        heat_to_room_K_per_min=[0.0] * n,
    )
    m = compute_metrics(series, transient_start_s=0.0)
    # Direction reversals: up->down, down->up, up->down  = 3
    assert m.valve_cycle_count == 3


def test_integral_valve_percent_minutes():
    """A 60-minute window at 50 percent yields 3000 percent·min by trapezoidal integration."""
    n = 61  # samples
    series = TimeSeries(
        t_s=[i * 60.0 for i in range(n)],
        T_room=[20.0] * n,
        T_setpoint=[20.0] * n,
        valve_percent=[50.0] * n,
        heat_to_room_K_per_min=[0.0] * n,
    )
    m = compute_metrics(series, transient_start_s=0.0)
    assert abs(m.integral_valve_percent_minutes - 3000.0) < 1e-6


def test_delivered_heat_integrates_the_heat_rate():
    """A 60-minute window at 0.02 K/min delivers 1.2 K."""
    n = 61
    series = TimeSeries(
        t_s=[i * 60.0 for i in range(n)],
        T_room=[20.0] * n,
        T_setpoint=[20.0] * n,
        valve_percent=[0.0] * n,
        heat_to_room_K_per_min=[0.02] * n,
    )
    m = compute_metrics(series, transient_start_s=0.0)
    assert m.delivered_heat_K == pytest.approx(1.2)


def test_delivered_heat_charges_each_step_its_starting_rate():
    """The rate recorded at a step's start covers that step; the last one none."""
    series = TimeSeries(
        t_s=[0.0, 60.0, 120.0],
        T_room=[20.0] * 3,
        T_setpoint=[20.0] * 3,
        valve_percent=[0.0] * 3,
        heat_to_room_K_per_min=[0.0, 1.0, 5.0],
    )
    m = compute_metrics(series, transient_start_s=0.0)
    assert m.delivered_heat_K == pytest.approx(1.0)


def test_timeseries_rejects_mismatched_lengths():
    """Parallel arrays of different length fail at construction."""
    with pytest.raises(ValueError):
        TimeSeries(
            t_s=[0.0, 30.0],
            T_room=[20.0],
            T_setpoint=[21.0, 21.0],
            valve_percent=[0.0, 0.0],
            heat_to_room_K_per_min=[0.0, 0.0],
        )


def test_timeseries_rejects_non_monotonic_time():
    """Non-increasing timestamps fail at construction."""
    with pytest.raises(ValueError):
        TimeSeries(
            t_s=[0.0, 30.0, 30.0],
            T_room=[20.0, 20.0, 20.0],
            T_setpoint=[21.0, 21.0, 21.0],
            valve_percent=[0.0, 0.0, 0.0],
            heat_to_room_K_per_min=[0.0, 0.0, 0.0],
        )


def test_imbalance_clips_interval_straddling_transient_start():
    """Only the post-transient portion of a straddling interval is charged."""
    series = TimeSeries(
        t_s=[0.0, 3600.0],
        T_room=[22.0, 22.0],
        T_setpoint=[21.0, 21.0],
        valve_percent=[0.0, 0.0],
        heat_to_room_K_per_min=[0.0, 0.0],
    )
    m = compute_metrics(series, transient_start_s=1800.0)
    # 1 K error over the half hour after the transient start → 0.5 K·h.
    assert m.time_above_setpoint_K_h == pytest.approx(0.5)


def test_timeseries_rejects_non_finite_values():
    """NaN or infinite samples fail at construction."""
    with pytest.raises(ValueError):
        TimeSeries(
            t_s=[0.0, float("nan")],
            T_room=[20.0, 20.0],
            T_setpoint=[21.0, 21.0],
            valve_percent=[0.0, 0.0],
            heat_to_room_K_per_min=[0.0, 0.0],
        )
    with pytest.raises(ValueError):
        TimeSeries(
            t_s=[0.0, 30.0],
            T_room=[20.0, float("inf")],
            T_setpoint=[21.0, 21.0],
            valve_percent=[0.0, 0.0],
            heat_to_room_K_per_min=[0.0, 0.0],
        )


def test_discomfort_counts_only_the_deviation_beyond_the_band():
    """0.5 K off the setpoint for an hour is 0.3 K·h beyond the 0.2 K band."""
    series = _flat_series(21.5, 21.0, n=61)
    m = compute_metrics(series, transient_start_s=0.0)
    assert m.discomfort_K_h == pytest.approx(0.3)
    assert m.transient_duration_h == pytest.approx(1.0)


@pytest.mark.parametrize("value", [21.15, 20.85])
def test_discomfort_is_zero_inside_the_band(value: float):
    m = compute_metrics(_flat_series(value, 21.0, n=61), transient_start_s=0.0)
    assert m.discomfort_K_h == 0.0


def test_discomfort_counts_both_directions():
    above = compute_metrics(_flat_series(21.5, 21.0, n=61), transient_start_s=0.0)
    below = compute_metrics(_flat_series(20.5, 21.0, n=61), transient_start_s=0.0)
    assert below.discomfort_K_h == pytest.approx(above.discomfort_K_h)


def test_discomfort_starts_at_the_transient():
    m = compute_metrics(_flat_series(21.5, 21.0, n=61), transient_start_s=1800.0)
    assert m.discomfort_K_h == pytest.approx(0.15)
    assert m.transient_duration_h == pytest.approx(0.5)


def test_discomfort_charges_each_interval_its_starting_sample():
    """A step recorded at the end of an interval is not charged to it."""
    series = TimeSeries(
        t_s=[0.0, 3600.0, 7200.0],
        T_room=[21.0, 21.5, 21.5],
        T_setpoint=[21.0, 21.0, 21.0],
        valve_percent=[0.0, 0.0, 0.0],
        heat_to_room_K_per_min=[0.0, 0.0, 0.0],
    )
    m = compute_metrics(series, transient_start_s=0.0)
    # In band for the first hour, 0.3 K beyond it for the second.
    assert m.discomfort_K_h == pytest.approx(0.3)
