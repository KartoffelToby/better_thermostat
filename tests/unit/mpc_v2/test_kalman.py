"""Unit tests for the 2-state Kalman observer."""

from __future__ import annotations

import math

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.kalman import (
    KalmanObserver,
    KalmanParams,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)


def _make_observer(plant: PlantModelRC2) -> KalmanObserver:
    """Build a Kalman observer on the given plant."""
    return KalmanObserver(plant, KalmanParams())


def test_initialise_seeds_x_hat() -> None:
    """initialise seeds the state estimate with the given vector."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([21.0, 35.0]))
    assert float(obs.x_hat[0]) == 21.0
    assert float(obs.x_hat[1]) == 35.0


def test_observer_reconstructs_T_rad_from_T_room_measurements() -> None:
    """Drive the truth model, feed only T_room into the observer, verify T_rad recovery."""
    plant_true = PlantModelRC2(
        PlantParams(tau_room_min=120.0, tau_rad_min=8.0), dt_s=30.0
    )
    plant_model = PlantModelRC2(
        PlantParams(tau_room_min=120.0, tau_rad_min=8.0), dt_s=30.0
    )
    obs = _make_observer(plant_model)
    x_true = np.array([19.0, 19.0])
    obs.initialise(np.array([19.0, 19.0]))
    rng = np.random.default_rng(0)
    for _ in range(800):
        u = 0.5
        x_true = plant_true.discrete_step(x_true, u=u, T_outdoor_C=5.0)
        y_meas = float(x_true[0]) + rng.normal(0, 0.02)
        obs.update(y_meas, u=u, T_outdoor_C=5.0)
    # After convergence, the observer's T_rad estimate tracks truth within
    # ~0.5 K despite only seeing T_room with sensor noise.
    assert abs(float(obs.x_hat[1]) - float(x_true[1])) < 1.0


def test_innovation_matches_measurement_minus_predicted_y() -> None:
    """The innovation equals the measurement minus the predicted output."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 30.0]))
    y_meas = 20.5
    A, B, d = plant.linearised_AB(T_outdoor_C=5.0, T_rad_op_C=30.0)
    x_pred = A @ obs.x_hat + B.flatten() * 0.3 + d
    expected = y_meas - float(x_pred[0])
    assert abs(obs.innovation(y_meas, u=0.3, T_outdoor_C=5.0) - expected) < 1e-12


def test_observer_uses_actual_elapsed_time() -> None:
    """A sparse HA event advances the model by its full interval, not 30 s."""
    plant = PlantModelRC2(PlantParams(tau_room_min=120.0, tau_rad_min=8.0), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 35.0]))
    y_meas = 20.2
    A, B, d = plant.linearised_AB(5.0, 35.0, dt_s=300.0)
    expected = y_meas - float((A @ obs.x_hat + B.flatten() * 0.2 + d)[0])

    assert obs.innovation(y_meas, u=0.2, T_outdoor_C=5.0, dt_s=300.0) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(
    ("dt_pattern_s", "steps"),
    [((30.0,), 800), ((300.0, 300.0, 1800.0), 400)],
    ids=["steady-cadence", "sparse-cadence"],
)
def test_covariance_stays_symmetric_and_positive_semidefinite(
    dt_pattern_s: tuple[float, ...], steps: int
) -> None:
    """Every update must leave a covariance a Kalman gain can be taken from.

    Each step folds its covariance into the next, so a step that loses
    symmetry or positive semidefiniteness only shows up once the sequence
    carries it forward. The bound is a rounding bound: the two off-diagonal
    entries evaluate one algebraic expression in a different operation order,
    so they may differ by a few units in the last place of the covariance
    scale, and no more.
    """
    eps = float(np.finfo(float).eps)
    rounding_ulps = 64.0
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 22.0]))

    for k in range(steps):
        dt_s = dt_pattern_s[k % len(dt_pattern_s)]
        u = 0.9 if (k // 20) % 2 == 0 else 0.05
        y_meas = 20.0 + 2.0 * math.sin(k / 9.0) + 0.002 * k
        obs.update(y_meas, u=u, T_outdoor_C=-5.0, dt_s=dt_s)

        P = obs.P
        scale = float(np.trace(P))
        assert math.isfinite(scale)
        tolerance = rounding_ulps * eps * scale
        assert abs(float(P[0, 1] - P[1, 0])) <= tolerance
        assert float(np.linalg.eigvalsh(P).min()) >= -tolerance
