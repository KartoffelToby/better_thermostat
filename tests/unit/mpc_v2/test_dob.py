"""Unit tests for the disturbance observer (EMA over Kalman innovations)."""

from __future__ import annotations

import math

import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.dob import (
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


def test_estimate_climbs_to_a_standing_disturbance_along_its_time_constant() -> None:
    """A standing disturbance is approached geometrically, not in one step.

    The observer is a first-order EMA with weight ``dt_s / tau_s``, so a
    constant innovation rate ``r`` starting from zero stands at
    ``r * (1 - (1 - dt_s / tau_s) ** k)`` after ``k`` steps. The interval is a
    twentieth of the time constant, which keeps the weight far below the
    scale a fixed floor would impose, and the rate is an order of magnitude
    under ``max_abs_K_per_min`` so the whole sequence runs inside the bound
    and the EMA alone decides every value.
    """
    tau_s, dt_s, steps = 600.0, 30.0, 120
    params = DobParams(tau_s=tau_s, max_abs_K_per_min=0.05)
    weight = dt_s / tau_s
    innovation_K = 0.001
    rate_K_per_min = innovation_K / (dt_s / 60.0)
    assert rate_K_per_min < params.max_abs_K_per_min / 10.0

    dob = DisturbanceObserver(params)
    estimates = [dob.update(innovation_K, dt_s=dt_s) for _ in range(steps)]

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
        """Innovation in K and its interval in seconds for step ``k``."""
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


def test_saturated_estimate_decays_once_innovations_stop() -> None:
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
