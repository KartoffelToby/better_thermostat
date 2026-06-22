"""Schedule-builder unit tests."""

from __future__ import annotations

import math

import pytest

from tests.benchmark import schedules


def test_step_switches_at_threshold():
    """Step switches at threshold."""
    f = schedules.step(t_threshold_s=100.0, before=18.0, after=21.0)
    assert f(0.0) == 18.0
    assert f(99.999) == 18.0
    assert f(100.0) == 21.0
    assert f(1000.0) == 21.0


def test_constant_is_time_independent():
    """Constant is time independent."""
    f = schedules.constant(5.5)
    for t in (0.0, 60.0, 3600.0, 86400.0):
        assert f(t) == 5.5


def test_pulse_returns_value_inside_window_and_default_outside():
    """Pulse returns value inside window and default outside."""
    f = schedules.pulse(t_start_s=100.0, t_end_s=200.0, value=1.0, default=-0.5)
    assert f(0.0) == -0.5
    assert f(99.9) == -0.5
    assert f(100.0) == 1.0
    assert f(150.0) == 1.0
    assert f(199.9) == 1.0
    assert f(200.0) == -0.5  # half-open right
    assert f(1000.0) == -0.5


def test_pulse_bool_returns_bool():
    """Pulse bool returns bool."""
    f = schedules.pulse_bool(t_start_s=10.0, t_end_s=20.0)
    assert f(0.0) is False
    assert f(10.0) is True
    assert f(15.0) is True
    assert f(20.0) is False


def test_ramp_interpolates_between_endpoints_and_holds_after():
    """Ramp interpolates between endpoints and holds after."""
    f = schedules.ramp(t_start_s=0.0, t_end_s=100.0, start_value=10.0, end_value=20.0)
    assert f(-1.0) == 10.0
    assert f(0.0) == 10.0
    assert math.isclose(f(50.0), 15.0)
    assert f(100.0) == 20.0
    assert f(1000.0) == 20.0


def test_ramp_after_override():
    """Ramp after override."""
    f = schedules.ramp(
        t_start_s=0.0, t_end_s=100.0, start_value=10.0, end_value=20.0, after=5.0
    )
    assert f(50.0) == 15.0
    assert f(100.0) == 5.0
    assert f(1000.0) == 5.0


def test_piecewise_step_walks_thresholds_in_order():
    """Piecewise step walks thresholds in order."""
    f = schedules.piecewise_step(
        pairs=[(100.0, 18.0), (200.0, 22.0), (300.0, 20.0)], initial=16.0
    )
    assert f(0.0) == 16.0
    assert f(99.9) == 16.0
    assert f(100.0) == 18.0
    assert f(199.9) == 18.0
    assert f(200.0) == 22.0
    assert f(300.0) == 20.0
    assert f(10000.0) == 20.0


def test_piecewise_step_sorts_unsorted_input():
    """Piecewise step sorts unsorted input."""
    f = schedules.piecewise_step(
        pairs=[(300.0, 20.0), (100.0, 18.0), (200.0, 22.0)], initial=16.0
    )
    assert f(150.0) == 18.0
    assert f(250.0) == 22.0
    assert f(350.0) == 20.0


def test_sinus_diurnal_reaches_min_and_max():
    """Sinus diurnal reaches min and max."""
    f = schedules.sinus_diurnal(
        min_value=-5.0, max_value=5.0, period_h=24.0, phase_min_h=6.0
    )
    # Minimum at simulation hour 6.
    assert math.isclose(f(6.0 * 3600.0), -5.0, abs_tol=1e-9)
    # Maximum 12 h later.
    assert math.isclose(f(18.0 * 3600.0), 5.0, abs_tol=1e-9)
    # Mid value at the transition quarter-points.
    assert math.isclose(f(0.0 * 3600.0), 0.0, abs_tol=1e-9)


def test_stochastic_windows_deterministic_and_within_duration():
    """Stochastic windows deterministic and within duration."""
    f = schedules.stochastic_windows(
        seed=42,
        count=5,
        duration_s=10 * 3600.0,
        min_duration_s=300.0,
        max_duration_s=600.0,
    )
    # Determinism — same seed → identical schedule.
    g = schedules.stochastic_windows(
        seed=42,
        count=5,
        duration_s=10 * 3600.0,
        min_duration_s=300.0,
        max_duration_s=600.0,
    )
    samples_f = [f(t * 60.0) for t in range(600)]
    samples_g = [g(t * 60.0) for t in range(600)]
    assert samples_f == samples_g
    # At least one window must actually open and at least one moment must be closed.
    assert any(samples_f)
    assert not all(samples_f)


def test_stochastic_windows_handles_short_slots_gracefully():
    """Stochastic windows handles short slots gracefully."""
    # max_duration_s > slot_s → fallback path: t_start = i * slot_s, dur clipped by sampling.
    f = schedules.stochastic_windows(
        seed=1, count=10, duration_s=100.0, min_duration_s=20.0, max_duration_s=50.0
    )
    # At least one tick is True somewhere in the window.
    assert any(f(t) for t in range(0, 100))


def test_solar_trapezoid_envelope():
    """Solar trapezoid envelope."""
    f = schedules.solar_trapezoid(
        t_rise_start=100.0,
        t_rise_end=200.0,
        t_fall_start=300.0,
        t_fall_end=400.0,
        peak=2.0,
    )
    assert f(50.0) == 0.0  # before rise
    assert math.isclose(f(150.0), 1.0)  # half-rise
    assert f(200.0) == 2.0  # plateau start
    assert f(250.0) == 2.0  # mid-plateau
    assert math.isclose(f(350.0), 1.0)  # half-fall
    assert f(400.0) == 0.0  # past fall
    assert f(1000.0) == 0.0  # well past


def test_ramp_rejects_non_positive_span():
    """A zero- or negative-length ramp window is rejected."""
    with pytest.raises(ValueError):
        schedules.ramp(100.0, 100.0, 0.0, 1.0)
    with pytest.raises(ValueError):
        schedules.ramp(200.0, 100.0, 0.0, 1.0)


def test_stochastic_windows_rejects_non_positive_count():
    """Count <= 0 is rejected instead of dividing by zero."""
    with pytest.raises(ValueError):
        schedules.stochastic_windows(seed=1, count=0, duration_s=3600.0)


def test_stochastic_windows_rejects_inconsistent_duration_bounds():
    """min_duration_s > max_duration_s is rejected."""
    with pytest.raises(ValueError):
        schedules.stochastic_windows(
            seed=1,
            count=2,
            duration_s=3600.0,
            min_duration_s=600.0,
            max_duration_s=300.0,
        )


def test_stochastic_windows_do_not_spill_into_next_slot():
    """Short slots clamp event durations so events stay non-overlapping."""
    count = 6
    slot_s = 600.0
    sched = schedules.stochastic_windows(
        seed=7,
        count=count,
        duration_s=count * slot_s,
        min_duration_s=300.0,
        max_duration_s=1800.0,  # far longer than one slot
    )
    edges = 0
    prev = False
    t = 0.0
    while t < count * slot_s:
        cur = sched(t)
        if cur and not prev:
            edges += 1
        prev = cur
        t += 1.0
    # Spilling events would merge with their successors → fewer edges.
    assert edges == count
