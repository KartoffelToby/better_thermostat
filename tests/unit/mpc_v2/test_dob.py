"""Unit tests for the disturbance observer (EMA over Kalman room corrections)."""

from __future__ import annotations

import math

import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.dob import (
    CONTROL_TICK_S,
    DisturbanceObserver,
    DobParams,
)


def test_non_positive_dt_leaves_estimate_unchanged() -> None:
    """Zero or negative dt is a no-op on the disturbance estimate."""
    dob = DisturbanceObserver(DobParams(tau_s=600.0))
    dob.update(0.5, dt_s=300.0)
    before = dob.D_hat_K_per_min
    assert dob.update(1.0, dt_s=0.0) == before
    assert dob.update(1.0, dt_s=-1.0) == before
    assert dob.D_hat_K_per_min == before


def test_near_zero_dt_contribution_is_dt_proportional() -> None:
    """A tiny-dt update contributes ``60 * innovation / tau_s``, not more.

    The innovation rate grows as ``1/dt``, so the EMA weight must scale with
    ``dt`` for the product to stay bounded as ``dt -> 0``.
    """
    tau_s = 600.0
    innovation_K = 0.5
    dob = DisturbanceObserver(DobParams(tau_s=tau_s))
    dob.update(innovation_K, dt_s=0.001)
    assert dob.D_hat_K_per_min == pytest.approx(60.0 * innovation_K / tau_s)


def test_two_near_zero_dt_updates_do_not_amplify() -> None:
    """Two updates 1 ms apart stay within twice one update's contribution.

    A shared group controller is stepped once per TRV within the same control
    pass, so back-to-back near-zero dt updates are a realistic input; they
    must accumulate at most linearly instead of blowing up the estimate.
    """
    tau_s = 600.0
    innovation_K = 0.5
    per_update = 60.0 * innovation_K / tau_s

    single = DisturbanceObserver(DobParams(tau_s=tau_s))
    single.update(innovation_K, dt_s=0.001)

    double = DisturbanceObserver(DobParams(tau_s=tau_s))
    double.update(innovation_K, dt_s=0.001)
    double.update(innovation_K, dt_s=0.001)

    assert single.D_hat_K_per_min == pytest.approx(per_update)
    assert double.D_hat_K_per_min <= 2.0 * per_update
    assert double.D_hat_K_per_min <= 2.0 * single.D_hat_K_per_min


def test_regular_dt_converges_towards_innovation_rate() -> None:
    """Repeated same-sign innovations at tau-scale dt approach the rate."""
    tau_s = 600.0
    dt_s = 300.0
    innovation_K = 0.5
    innov_rate = innovation_K / (dt_s / 60.0)

    dob = DisturbanceObserver(DobParams(tau_s=tau_s))
    for _ in range(50):
        dob.update(innovation_K, dt_s=dt_s)

    assert dob.D_hat_K_per_min == pytest.approx(innov_rate, rel=1e-6)


def test_planning_rate_follows_the_estimate_along_its_slower_time_constant() -> None:
    """The planning reading lags the estimate by ``planning_tau_s``.

    With the estimate pinned at 0.02 K/min by a zero-length EMA, one update
    of ``dt_s`` moves the slow reading by ``dt_s / planning_tau_s`` of the
    gap, and the deadband then takes ``planning_deadband`` off its magnitude.
    """
    params = DobParams(tau_s=1.0, planning_tau_s=3600.0, planning_deadband=0.001)
    dob = DisturbanceObserver(params)

    dob.update(0.02 * 5.0, dt_s=300.0)

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
def test_a_reading_after_a_pause_leaves_both_estimates_alone(dt_s: float) -> None:
    """A correction that closes a pause in the control loop is not folded in.

    Beyond ``max_reading_interval_s`` the controller did not run in between
    (window open, heating off, restart), so what the room did then says
    nothing about a standing disturbance.
    """
    dob = DisturbanceObserver(DobParams())
    dob.update(0.02 * 5.0, dt_s=300.0)
    fast, slow = dob.D_hat_K_per_min, dob.planning_filtered
    assert dt_s > dob.params.max_reading_interval_s

    dob.update(-1.5, dt_s=dt_s)

    assert (dob.D_hat_K_per_min, dob.planning_filtered) == (fast, slow)


def test_a_reading_at_the_pause_limit_is_still_folded_in() -> None:
    """An interval of exactly ``max_reading_interval_s`` is a regular reading."""
    dob = DisturbanceObserver(DobParams())

    dob.update(0.02 * 7.5, dt_s=dob.params.max_reading_interval_s)

    assert dob.D_hat_K_per_min > 0.0
    assert dob.planning_filtered > 0.0


def test_the_pause_limit_sits_between_one_and_two_control_ticks() -> None:
    """A regular cycle is folded in, one skipped control tick is a pause.

    The limit follows the periodic tick that drives calibration, so a change
    of that tick moves it along instead of silencing the observer.
    """
    limit = DobParams().max_reading_interval_s

    assert CONTROL_TICK_S < limit < 2.0 * CONTROL_TICK_S
