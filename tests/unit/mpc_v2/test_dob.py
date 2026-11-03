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
    """Repeated same-sign innovations at tau-scale dt approach the rate.

    The innovation is sized so its rate stays inside ``max_abs_K_per_min``,
    leaving the EMA rather than the bound to decide where the estimate ends
    up; the bound itself is covered by
    ``test_large_sensor_jump_is_bounded_before_feed_forward``.
    """
    tau_s = 600.0
    dt_s = 300.0
    innovation_K = 0.1
    innov_rate = innovation_K / (dt_s / 60.0)
    assert innov_rate < DobParams().max_abs_K_per_min

    dob = DisturbanceObserver(DobParams(tau_s=tau_s))
    for _ in range(50):
        dob.update(innovation_K, dt_s=dt_s)

    assert dob.D_hat_K_per_min == pytest.approx(innov_rate, rel=1e-6)


def test_large_sensor_jump_is_bounded_before_feed_forward() -> None:
    """Quantised sensor jumps cannot create an implausible steady-state load."""
    dob = DisturbanceObserver(DobParams(tau_s=1.0, max_abs_K_per_min=0.05))
    assert dob.update(5.0, dt_s=60.0) == pytest.approx(0.05)


def test_both_readings_stay_inside_the_bound_a_restore_applies() -> None:
    """A running observer holds the readings a restart would restore.

    The restore takes a stored reading at ``max_abs_K_per_min``, so the live
    readings must not exceed it either, or a restart would change the
    disturbance the plan uses.
    """
    dob = DisturbanceObserver(DobParams())
    bound = dob.params.max_abs_K_per_min
    for _ in range(20):
        dob.update(0.5, dt_s=300.0)
    live = (dob.D_hat_K_per_min, dob.planning_filtered)

    restored = DisturbanceObserver(DobParams())
    restored.restore(*live)

    assert live[0] == pytest.approx(bound)
    assert 0.0 < live[1] <= bound
    assert (restored.D_hat_K_per_min, restored.planning_filtered) == live


def test_estimate_climbs_to_a_standing_disturbance_along_its_time_constant() -> None:
    """A standing disturbance is approached geometrically, not in one step.

    The observer is a first-order EMA with weight ``dt_s / tau_s``, so a
    constant correction rate ``r`` starting from zero stands at
    ``r * (1 - (1 - dt_s / tau_s) ** k)`` after ``k`` steps. The interval is a
    twentieth of the time constant, which keeps the weight far below the
    scale a fixed floor would impose, and the rate is an order of magnitude
    under ``max_abs_K_per_min`` so the whole sequence runs inside the bound
    and the EMA alone decides every value.
    """
    tau_s, dt_s, steps = 600.0, 30.0, 120
    params = DobParams(tau_s=tau_s, max_abs_K_per_min=0.05)
    weight = dt_s / tau_s
    correction_K = 0.001
    rate_K_per_min = correction_K / (dt_s / 60.0)
    assert rate_K_per_min < params.max_abs_K_per_min / 10.0

    dob = DisturbanceObserver(params)
    estimates = [dob.update(correction_K, dt_s=dt_s) for _ in range(steps)]

    assert estimates == pytest.approx(
        [rate_K_per_min * (1.0 - (1.0 - weight) ** k) for k in range(1, steps + 1)]
    )
    # The bound must never take over, or the geometry above says nothing.
    assert max(estimates) < params.max_abs_K_per_min
    assert len(set(estimates)) == steps


def test_estimate_stays_inside_its_bound_across_a_long_run() -> None:
    """The bound holds at every step and clips both signs, whatever came before.

    A single bounded update says nothing about a run: the estimate feeds back
    into itself. The run drives a gentle standing load, a sensor jump far past
    the bound, a quiet stretch on a faster cadence, an opposite jump and a
    mixed-interval tail, so the estimate spends most of its steps moving
    freely and still reaches both limits.
    """
    params = DobParams(tau_s=600.0, max_abs_K_per_min=0.05)
    bound = params.max_abs_K_per_min
    dob = DisturbanceObserver(params)
    steps = 400

    def episode(k: int) -> tuple[float, float]:
        """Correction in K and its interval in seconds for step ``k``."""
        if k < 100:
            return 0.02, 300.0
        if k < 150:
            return 4.0, 300.0
        if k < 250:
            return 0.0, 30.0
        if k < 300:
            return -4.0, 300.0
        return (0.01 if k % 2 else -0.015), (30.0, 300.0, 1800.0, 0.5)[k % 4]

    estimates = [dob.update(*episode(k)) for k in range(steps)]

    assert all(math.isfinite(estimate) for estimate in estimates)
    assert all(abs(estimate) <= bound for estimate in estimates)
    assert max(estimates) == pytest.approx(bound)
    assert min(estimates) == pytest.approx(-bound)
    # Most of the run has to happen away from the limits, or the bound would
    # be the only thing the sequence ever reports.
    assert sum(abs(estimate) < 0.99 * bound for estimate in estimates) > steps // 2


def test_saturated_estimate_decays_once_corrections_stop() -> None:
    """A saturated estimate must relax again instead of staying parked.

    The EMA weight follows from ``dt_s`` and ``tau_s``, so each quiet step has
    to scale the estimate by exactly ``1 - dt_s / tau_s``.
    """
    tau_s, dt_s = 600.0, 300.0
    params = DobParams(tau_s=tau_s, max_abs_K_per_min=0.05)
    weight = dt_s / tau_s

    dob = DisturbanceObserver(params)
    dob.update(5.0, dt_s=dt_s)
    saturated = dob.D_hat_K_per_min
    assert saturated == pytest.approx(params.max_abs_K_per_min)

    for quiet_steps in range(1, 21):
        dob.update(0.0, dt_s=dt_s)
        assert dob.D_hat_K_per_min == pytest.approx(
            saturated * (1.0 - weight) ** quiet_steps
        )


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
