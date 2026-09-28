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


def test_room_correction_is_the_move_from_prediction_towards_measurement() -> None:
    """``room_correction`` is how far ``update`` moved the room off its prediction.

    The move points towards the measurement and stops short of it, since the
    filter weighs the measurement against its own prediction.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 30.0]))
    predicted = plant.discrete_step(obs.x_hat, 0.3, 5.0)
    y_meas = 20.5

    x_hat = obs.update(y_meas, u=0.3, T_outdoor_C=5.0)

    residual = y_meas - float(predicted[0])
    assert obs.room_correction == pytest.approx(float(x_hat[0] - predicted[0]))
    assert 0.0 < obs.room_correction / residual < 1.0


def test_observer_uses_actual_elapsed_time() -> None:
    """A sparse HA event advances the model by its full interval, not 30 s.

    Five minutes between readings predict what ten consecutive 30-second
    plant steps under the held valve fraction reach, so a reading exactly
    there needs no correction, while one 30-second step falls short of it.
    """
    plant = PlantModelRC2(PlantParams(tau_room_min=120.0, tau_rad_min=8.0), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 35.0]))
    x = obs.x_hat.copy()
    for _ in range(10):
        x = plant.discrete_step(x, 0.2, 5.0)
    one_step = plant.discrete_step(obs.x_hat, 0.2, 5.0)
    assert float(x[0]) != pytest.approx(float(one_step[0]))

    x_hat = obs.update(float(x[0]), u=0.2, T_outdoor_C=5.0, dt_s=300.0)

    assert obs.room_correction == pytest.approx(0.0, abs=1e-12)
    np.testing.assert_allclose(x_hat, x)


def test_update_corrects_against_the_plants_own_prediction() -> None:
    """``update`` predicts with the plant's propagation over the elapsed time.

    The disturbance observer reads ``room_correction`` as the part of the
    residual the filter acted on, so the filter's prediction has to be the
    plant's own. Feeding back exactly what :meth:`PlantModelRC2.propagate`
    predicts leaves the estimate on that prediction with no correction, at
    every step of a run whose intervals and valve positions keep changing.
    The bound is a rounding bound: the correction scales a difference of a
    few units in the last place down, so the estimate moves by no more.
    """
    eps = float(np.finfo(float).eps)
    rounding_ulps = 64.0
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 45.0]))
    intervals_s = (30.0, 300.0, 1800.0, 90.0)

    estimates = []
    for k in range(200):
        dt_s = intervals_s[k % len(intervals_s)]
        u = 0.8 if (k // 7) % 2 == 0 else 0.1
        predicted_C = float(plant.propagate(obs.x_hat, u, -5.0, dt_s)[0])
        x_hat = obs.update(predicted_C, u=u, T_outdoor_C=-5.0, dt_s=dt_s)
        assert abs(float(x_hat[0]) - predicted_C) <= rounding_ulps * eps * abs(
            predicted_C
        )
        assert abs(obs.room_correction) <= rounding_ulps * eps * abs(predicted_C)
        estimates.append(float(x_hat[1]))

    # A radiator estimate that never left its seed would satisfy the above
    # whatever the two predictions did.
    assert len(set(estimates)) == len(estimates)
