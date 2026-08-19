"""Unit tests for the disturbance observer (EMA over Kalman innovations)."""

from __future__ import annotations

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
    """Repeated same-sign innovations at tau-scale dt approach the rate."""
    tau_s = 600.0
    dt_s = 300.0
    innovation_K = 0.5
    innov_rate = innovation_K / (dt_s / 60.0)

    dob = DisturbanceObserver(DobParams(tau_s=tau_s))
    for _ in range(50):
        dob.update(innovation_K, dt_s=dt_s)

    assert dob.D_hat_K_per_min == pytest.approx(innov_rate, rel=1e-6)


def test_large_sensor_jump_is_bounded_before_feed_forward() -> None:
    """Quantised sensor jumps cannot create an implausible steady-state load."""
    dob = DisturbanceObserver(DobParams(tau_s=1.0, max_abs_K_per_min=0.05))
    assert dob.update(5.0, dt_s=60.0) == pytest.approx(0.05)
