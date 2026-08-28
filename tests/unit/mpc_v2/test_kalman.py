"""Unit tests for the 2-state Kalman observer."""

from __future__ import annotations

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


def test_update_keeps_the_covariance_exactly_symmetric() -> None:
    """P stays symmetric bit for bit, not merely to within round-off."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 22.0]))
    for step in range(50):
        obs.update(20.1 + 0.01 * step, u=0.4, T_outdoor_C=5.0, dt_s=300.0)
        np.testing.assert_array_equal(obs.P, obs.P.T)


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param([[-1.0, 0.0], [0.0, 1.0]], id="negative-variance"),
        pytest.param([[1.0, 2.0], [2.0, 1.0]], id="symmetric-but-indefinite"),
        pytest.param([[0.01, 5.0], [0.0, 0.5]], id="asymmetric"),
        pytest.param([[0.01, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], id="3x3"),
        pytest.param([[0.01, float("nan")], [float("nan"), 1.0]], id="non-finite"),
        pytest.param([[0.01, 0.0], [0.0]], id="ragged"),
        pytest.param([], id="empty"),
    ],
)
def test_restore_covariance_refuses_a_matrix_that_is_not_a_covariance(
    stored: list[list[float]],
) -> None:
    """Only a finite, symmetric, positive semi-definite matrix may be adopted."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    default_P = obs.P.copy()

    assert obs.restore_covariance(stored) is False
    np.testing.assert_array_equal(obs.P, default_P)


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param([[0.02, 0.01], [0.01, 0.5]], id="positive-definite"),
        pytest.param([[1.0, 1.0], [1.0, 1.0]], id="positive-semi-definite"),
    ],
)
def test_restore_covariance_adopts_a_valid_matrix(stored: list[list[float]]) -> None:
    """A well-formed stored covariance replaces the construction default."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)

    assert obs.restore_covariance(stored) is True
    np.testing.assert_allclose(obs.P, np.array(stored))


def test_restored_covariance_keeps_the_estimate_bounded() -> None:
    """An indefinite covariance would blow the estimate up; a rejected one cannot."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 22.0]))
    obs.restore_covariance([[-1.0, 0.0], [0.0, 1.0]])

    for step in range(5):
        obs.update(20.1, u=0.4, T_outdoor_C=5.0, dt_s=300.0)
        assert abs(float(obs.x_hat[0]) - 20.1) < 5.0, f"diverged at step {step}"


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
