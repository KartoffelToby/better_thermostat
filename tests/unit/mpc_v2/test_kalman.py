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


def test_observer_reconstructs_T_rad_from_T_room_measurements() -> None:  # noqa: N802
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


@pytest.mark.parametrize(
    ("dt_pattern_s", "steps"),
    [((30.0,), 800), ((300.0, 300.0, 1800.0), 400)],
    ids=["steady-cadence", "sparse-cadence"],
)
def test_covariance_stays_between_its_noise_floor_and_the_sensor_variance(
    dt_pattern_s: tuple[float, ...], steps: int
) -> None:
    """Every update leaves a covariance the next Kalman gain can be taken from.

    Each step folds its covariance into the next, so a step that loses
    symmetry, over-shrinks or over-grows only shows up once the sequence
    carries it forward. Three bounds hold after every correction.

    Symmetry is a rounding bound: the two off-diagonal entries evaluate one
    algebraic expression in a different operation order, so they may differ
    by a few units in the last place of the covariance scale, and no more.

    The upper bound is the measurement: fusing an estimate with a reading of
    variance ``r_sensor`` cannot leave the measured channel more uncertain
    than that reading alone, so ``P[0, 0] < r_sensor``.

    The lower bound is this step's own process noise. In information form the
    correction is ``P_post⁻¹ = P_pred⁻¹ + Cᵀ·R⁻¹·C``, so the smallest
    eigenvalue can shrink no further than the parallel combination of
    ``λ_min(P_pred)`` and ``r_sensor``; and ``P_pred ⪰ Q · q_scale`` bounds
    ``λ_min(P_pred)`` by the smallest process-noise variance scaled to the
    interval actually elapsed. A filter that drops the covariance correction
    breaches the upper bound; one that drops the process noise, or scales it
    by a fixed step instead of the elapsed one, falls through the floor.
    """
    eps = float(np.finfo(float).eps)
    rounding_ulps = 64.0
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    params = obs.params
    q_min = min(params.q_room, params.q_rad)
    obs.initialise(np.array([20.0, 22.0]))

    for k in range(steps):
        dt_s = dt_pattern_s[k % len(dt_pattern_s)]
        u = 0.9 if (k // 20) % 2 == 0 else 0.05
        y_meas = 20.0 + 2.0 * math.sin(k / 9.0) + 0.002 * k
        obs.update(y_meas, u=u, T_outdoor_C=-5.0, dt_s=dt_s)

        P = obs.P
        scale = float(np.trace(P))
        assert math.isfinite(scale)
        assert abs(float(P[0, 1] - P[1, 0])) <= rounding_ulps * eps * scale
        assert float(P[0, 0]) < params.r_sensor
        q_step = q_min * dt_s / plant.dt_s
        noise_floor = q_step * params.r_sensor / (q_step + params.r_sensor)
        assert float(np.linalg.eigvalsh(P).min()) >= noise_floor


def test_reseeding_the_estimate_discards_the_confidence_of_the_run() -> None:
    """An estimate handed in from outside arrives without the run's confidence.

    A long run correlates the two channels and shrinks the radiator variance
    far below its prior. ``initialise`` replaces the estimate with a value the
    filter did not derive, so the covariance that described the old estimate
    must not survive: nothing links the two new channels yet, and the filter
    cannot stay more certain about the unmeasured radiator than it was before
    the run.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 22.0]))
    for k in range(400):
        obs.update(20.0 + 0.01 * k, u=0.5, T_outdoor_C=-5.0, dt_s=30.0)

    P_after_run = obs.P.copy()
    # The run has to have built something up, or the re-seed asserts nothing.
    assert float(P_after_run[0, 1]) != 0.0

    obs.initialise(np.array([21.0, 30.0]))

    assert float(obs.P[0, 1]) == 0.0
    assert float(obs.P[1, 0]) == 0.0
    assert float(obs.P[1, 1]) > float(P_after_run[1, 1])


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


def test_process_noise_stops_growing_with_the_prediction_at_settling_time() -> None:
    """A silence past the settling time adds no more uncertainty than one of it.

    The plant propagates at most over its settling time; the process noise
    follows the same span. Uncapped, a week-long gap beyond it would inflate
    the radiator variance tenfold, and the next ordinary room reading would
    then swing the unobserved radiator estimate by several kelvin.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    settling_s = plant.settling_time_s
    observers = {}
    for gap_s in (settling_s, 10.0 * settling_s):
        obs = _make_observer(plant)
        obs.initialise(np.array([20.0, 40.0]))
        obs.update(20.0, u=0.3, T_outdoor_C=5.0, dt_s=gap_s)
        obs.update(20.1, u=0.3, T_outdoor_C=5.0, dt_s=300.0)
        observers[gap_s] = obs

    at_settling, far_beyond = observers[settling_s], observers[10.0 * settling_s]
    np.testing.assert_allclose(far_beyond.P, at_settling.P)
    np.testing.assert_allclose(far_beyond.x_hat, at_settling.x_hat)
