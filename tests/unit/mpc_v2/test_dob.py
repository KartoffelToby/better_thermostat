"""Unit tests for the disturbance observer (bounded and planning readings)."""

from __future__ import annotations

import math

import pytest

from custom_components.better_thermostat.core.watchdog import CONTROL_TICK_S
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.dob import (
    DisturbanceObserver,
    DobParams,
)


@pytest.mark.parametrize("dt_s", [0.0, -1.0])
def test_non_positive_dt_leaves_the_planning_reading_unchanged(dt_s: float) -> None:
    """Zero or negative dt moves the fast estimate but not the planning reading."""
    dob = DisturbanceObserver(DobParams())
    dob.follow(0.01, dt_s=300.0)
    slow = dob.planning_filtered

    assert dob.follow(0.02, dt_s=dt_s) == pytest.approx(0.02)
    assert dob.planning_filtered == slow


@pytest.mark.parametrize("estimate", [5.0, -5.0])
def test_an_estimate_past_the_bound_is_clipped_before_feed_forward(
    estimate: float,
) -> None:
    """A wild estimate cannot create an implausible steady-state load."""
    dob = DisturbanceObserver(DobParams(max_abs_K_per_min=0.05))

    assert dob.follow(estimate, dt_s=60.0) == pytest.approx(
        math.copysign(0.05, estimate)
    )
    assert abs(dob.planning_filtered) <= 0.05


def test_planning_reading_approaches_a_held_estimate_along_its_time_constant() -> None:
    """A held estimate is approached geometrically with weight ``dt / planning_tau``."""
    params = DobParams(planning_tau_s=1200.0)
    dt_s, rate, steps = 300.0, 0.02, 12
    weight = dt_s / params.planning_tau_s
    dob = DisturbanceObserver(params)

    readings = []
    for _ in range(steps):
        dob.follow(rate, dt_s=dt_s)
        readings.append(dob.planning_filtered)

    assert readings == pytest.approx(
        [rate * (1.0 - (1.0 - weight) ** k) for k in range(1, steps + 1)]
    )
    assert len(set(readings)) == steps


def test_two_near_zero_dt_updates_do_not_amplify() -> None:
    """Two updates 1 ms apart move the planning reading at most twice as far.

    A shared group controller is stepped once per TRV within the same control
    pass, so back-to-back near-zero dt updates are a realistic input.
    """
    params = DobParams(planning_tau_s=1200.0)
    single = DisturbanceObserver(params)
    single.follow(0.02, dt_s=0.001)
    double = DisturbanceObserver(params)
    double.follow(0.02, dt_s=0.001)
    double.follow(0.02, dt_s=0.001)

    assert single.planning_filtered == pytest.approx(0.02 * 0.001 / 1200.0)
    assert double.planning_filtered <= 2.0 * single.planning_filtered


def test_planning_rate_follows_the_estimate_and_loses_the_deadband() -> None:
    """One update moves the slow reading by ``dt / planning_tau`` of the gap.

    The deadband then takes ``planning_deadband`` off its magnitude.
    """
    params = DobParams(planning_tau_s=3600.0, planning_deadband=0.001)
    dob = DisturbanceObserver(params)

    dob.follow(0.02, dt_s=300.0)

    assert dob.D_hat_K_per_min == pytest.approx(0.02)
    assert dob.planning_filtered == pytest.approx(0.02 * 300.0 / 3600.0)
    assert dob.planning_rate == pytest.approx(0.02 * 300.0 / 3600.0 - 0.001)


@pytest.mark.parametrize("rate", [0.0009, -0.0009, 0.001, -0.001])
def test_planning_rate_is_zero_inside_the_deadband(rate: float) -> None:
    """Rates no larger than ``planning_deadband`` plan as no disturbance."""
    dob = DisturbanceObserver(DobParams(planning_deadband=0.001))
    dob.planning_filtered = rate

    assert dob.planning_rate == 0.0


@pytest.mark.parametrize("rate", [0.003, -0.003])
def test_planning_rate_outside_the_deadband_shrinks_towards_zero(rate: float) -> None:
    """Outside the band the reading loses the band width, keeping its sign."""
    dob = DisturbanceObserver(DobParams(planning_deadband=0.001))
    dob.planning_filtered = rate

    assert dob.planning_rate == pytest.approx(math.copysign(0.002, rate))


@pytest.mark.parametrize("dt_s", [451.0, 1200.0, 86_400.0])
def test_a_reading_after_a_pause_leaves_the_planning_reading_alone(dt_s: float) -> None:
    """An interval that spans a pause of the control loop is not folded in.

    Beyond ``max_reading_interval_s`` the controller did not run in between
    (window open, heating off, restart).
    """
    dob = DisturbanceObserver(DobParams())
    dob.follow(0.02, dt_s=300.0)
    slow = dob.planning_filtered
    assert dt_s > dob.params.max_reading_interval_s

    dob.follow(-0.03, dt_s=dt_s)

    assert dob.planning_filtered == slow


def test_a_reading_at_the_pause_limit_is_still_folded_in() -> None:
    """An interval of exactly ``max_reading_interval_s`` is a regular reading."""
    dob = DisturbanceObserver(DobParams())

    dob.follow(0.02, dt_s=dob.params.max_reading_interval_s)

    assert dob.planning_filtered > 0.0


def test_the_pause_limit_sits_between_one_and_two_control_ticks() -> None:
    """A regular cycle is folded in, one skipped control tick is a pause.

    The limit follows the periodic tick that drives calibration, so a change
    of that tick moves it along instead of silencing the observer.
    """
    limit = DobParams().max_reading_interval_s

    assert CONTROL_TICK_S < limit < 2.0 * CONTROL_TICK_S


@pytest.mark.parametrize(
    ("estimate", "planning", "expected"),
    [
        pytest.param(0.2, 0.2, (0.05, 0.05), id="above"),
        pytest.param(-0.2, -0.2, (-0.05, -0.05), id="below"),
        pytest.param(0.01, None, (0.01, 0.0), id="no-planning-reading"),
    ],
)
def test_restore_bounds_both_readings(
    estimate: float, planning: float | None, expected: tuple[float, float]
) -> None:
    """Persisted readings are bounded; a missing planning reading starts at zero."""
    dob = DisturbanceObserver(DobParams(max_abs_K_per_min=0.05))

    dob.restore(estimate, planning)

    assert (dob.D_hat_K_per_min, dob.planning_filtered) == pytest.approx(expected)
