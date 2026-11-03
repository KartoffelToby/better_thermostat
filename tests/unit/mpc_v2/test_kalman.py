"""Unit tests for the Kalman observer (room, radiator, disturbance rate)."""

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
        x_true = plant_true.discrete_step(x_true, u=u, T_outdoor=5.0)
        y_meas = float(x_true[0]) + rng.normal(0, 0.02)
        obs.update(y_meas, u=u, T_outdoor=5.0)
    # After convergence, the observer's T_rad estimate tracks truth within
    # ~0.5 K despite only seeing T_room with sensor noise.
    assert abs(float(obs.x_hat[1]) - float(x_true[1])) < 1.0


def test_a_fresh_reading_moves_the_room_part_way_from_its_prediction() -> None:
    """The room estimate moves towards a fresh reading and stops short of it.

    The filter weighs the measurement against its own prediction. Part of the
    residual goes to the disturbance, in the residual's direction.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 30.0]))
    predicted = plant.discrete_step(obs.x_hat, 0.3, 5.0)
    y_meas = 20.5

    x_hat = obs.update(y_meas, u=0.3, T_outdoor=5.0)

    residual = y_meas - float(predicted[0])
    assert 0.0 < float(x_hat[0] - predicted[0]) / residual < 1.0
    assert obs.disturbance_K_per_min > 0.0


def test_update_keeps_the_covariance_exactly_symmetric() -> None:
    """P stays symmetric bit for bit, not merely to within round-off."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 22.0]))
    for step in range(50):
        obs.update(20.1 + 0.01 * step, u=0.4, T_outdoor=5.0, dt_s=300.0)
        np.testing.assert_array_equal(obs.P, obs.P.T)


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param([[-1.0, 0.0], [0.0, 1.0]], id="negative-variance"),
        pytest.param([[1.0, 2.0], [2.0, 1.0]], id="symmetric-but-indefinite"),
        pytest.param([[0.01, 5.0], [0.0, 0.5]], id="asymmetric"),
        pytest.param(np.eye(4).tolist(), id="4x4"),
        pytest.param(
            [[0.01, 0.0, 0.5], [0.0, 1.0, 0.0], [0.5, 0.0, 1e-4]], id="3x3-indefinite"
        ),
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
        pytest.param(
            [[0.02, 0.01, 1e-4], [0.01, 0.5, 0.0], [1e-4, 0.0, 1e-5]],
            id="positive-definite",
        ),
        pytest.param(
            [[1.0, 1.0, 0.0], [1.0, 1.0, 0.0], [0.0, 0.0, 0.0]],
            id="positive-semi-definite",
        ),
    ],
)
def test_restore_covariance_adopts_a_valid_matrix(stored: list[list[float]]) -> None:
    """A well-formed stored covariance replaces the construction default."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)

    assert obs.restore_covariance(stored) is True
    np.testing.assert_allclose(obs.P, np.array(stored))


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param([[0.02, 0.01], [0.01, 0.5]], id="positive-definite"),
        pytest.param([[1.0, 1.0], [1.0, 1.0]], id="positive-semi-definite"),
    ],
)
def test_a_covariance_without_the_disturbance_keeps_its_variance(
    stored: list[list[float]],
) -> None:
    """A 2×2 covariance holds the temperatures; the disturbance keeps its own.

    It carries no correlation between the temperatures and the disturbance,
    so the restored matrix has none either.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    disturbance_variance = float(obs.P[2, 2])

    assert obs.restore_covariance(stored) is True

    np.testing.assert_allclose(obs.P[:2, :2], np.array(stored))
    assert float(obs.P[2, 2]) == disturbance_variance
    np.testing.assert_array_equal(obs.P[:2, 2], [0.0, 0.0])
    np.testing.assert_array_equal(obs.P[2, :2], [0.0, 0.0])


def test_restored_covariance_keeps_the_estimate_bounded() -> None:
    """An indefinite covariance would blow the estimate up; a rejected one cannot."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 22.0]))
    obs.restore_covariance([[-1.0, 0.0], [0.0, 1.0]])

    for step in range(5):
        obs.update(20.1, u=0.4, T_outdoor=5.0, dt_s=300.0)
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

    x_hat = obs.update(float(x[0]), u=0.2, T_outdoor=5.0, dt_s=300.0)

    np.testing.assert_allclose(x_hat, x)
    assert obs.disturbance_K_per_min == pytest.approx(0.0, abs=1e-12)


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
    correction is ``P_post⁻¹ = P_pred⁻¹ + Cᵀ·R⁻¹·C``, and ``P_pred ⪰ Q ·
    q_scale``, so ``P_post`` dominates ``(Q·q_scale)⁻¹ + Cᵀ·R⁻¹·C`` inverted:
    the room's process noise in parallel with ``r_sensor``, the others as
    they are. Its temperature block, and with it the temperature block of
    ``P_post``, keeps the smaller of the two temperature process-noise
    variances in parallel with ``r_sensor`` as the floor of its smallest
    eigenvalue, scaled to the interval actually elapsed. A filter that drops
    the covariance correction breaches the upper bound; one that drops the
    process noise, or scales it by a fixed step instead of the elapsed one,
    falls through the floor.
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
        obs.update(y_meas, u=u, T_outdoor=-5.0, dt_s=dt_s)

        P = obs.P
        scale = float(np.trace(P))
        assert math.isfinite(scale)
        assert abs(float(P[0, 1] - P[1, 0])) <= rounding_ulps * eps * scale
        assert float(P[0, 0]) < params.r_sensor
        q_step = q_min * dt_s / plant.dt_s
        noise_floor = q_step * params.r_sensor / (q_step + params.r_sensor)
        assert float(np.linalg.eigvalsh(P[:2, :2]).min()) >= noise_floor
        assert float(P[2, 2]) > 0.0


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
        obs.update(20.0 + 0.01 * k, u=0.5, T_outdoor=-5.0, dt_s=30.0)

    P_after_run = obs.P.copy()
    # The run has to have built something up, or the re-seed asserts nothing.
    assert float(P_after_run[0, 1]) != 0.0

    obs.initialise(np.array([21.0, 30.0]))

    assert float(obs.P[0, 1]) == 0.0
    assert float(obs.P[1, 0]) == 0.0
    assert float(obs.P[1, 1]) > float(P_after_run[1, 1])


def test_update_corrects_against_the_plants_own_prediction() -> None:
    """``update`` predicts with the plant's propagation over the elapsed time.

    Feeding back exactly what :meth:`PlantModelRC2.propagate` predicts under
    the current disturbance leaves the estimate on that prediction with no
    correction, at every step of a run whose intervals and valve positions
    keep changing, and leaves the disturbance at zero. The bound is a
    rounding bound: the correction scales a difference of a few units in the
    last place down, so the estimate moves by no more.
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
        predicted_room_temperature = float(
            plant.propagate(obs.x_hat, u, -5.0, dt_s, obs.disturbance_K_per_min)[0]
        )
        x_hat = obs.update(predicted_room_temperature, u=u, T_outdoor=-5.0, dt_s=dt_s)
        assert abs(
            float(x_hat[0]) - predicted_room_temperature
        ) <= rounding_ulps * eps * abs(predicted_room_temperature)
        assert abs(obs.disturbance_K_per_min) <= 1e-9
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
        obs.update(20.0, u=0.3, T_outdoor=5.0, dt_s=gap_s)
        obs.update(20.1, u=0.3, T_outdoor=5.0, dt_s=300.0)
        observers[gap_s] = obs

    at_settling, far_beyond = observers[settling_s], observers[10.0 * settling_s]
    np.testing.assert_allclose(far_beyond.P, at_settling.P)
    np.testing.assert_allclose(far_beyond.x_hat, at_settling.x_hat)


def test_the_disturbance_converges_to_a_standing_load() -> None:
    """A constant heat load the model misses ends up in the disturbance state.

    The truth plant carries a load of 0.01 K/min that the model lacks. The
    filter sees only the room; after a day of five-minute readings its
    disturbance estimate stands at the load, and its room estimate on the
    room, which a filter without the disturbance would trail.
    """
    load_K_per_min = 0.01
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    x_true = np.array([20.0, 30.0])
    obs.initialise(x_true)
    for _ in range(288):
        for _ in range(10):
            x_true = plant.discrete_step(x_true, 0.3, 5.0, load_K_per_min)
        obs.update(float(x_true[0]), u=0.3, T_outdoor=5.0, dt_s=300.0)

    assert obs.disturbance_K_per_min == pytest.approx(load_K_per_min, rel=0.05)
    assert float(obs.x_hat[0]) == pytest.approx(float(x_true[0]), abs=0.01)


def test_the_disturbance_estimate_stays_inside_its_bound() -> None:
    """A reading far off the model cannot push the disturbance past its bound."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = KalmanObserver(plant, KalmanParams(), disturbance_bound_K_per_min=0.05)
    obs.initialise(np.array([20.0, 30.0]))
    for k in range(40):
        obs.update(20.0 + (5.0 if k % 2 else -5.0) * k, u=0.3, T_outdoor=5.0)
        assert abs(obs.disturbance_K_per_min) <= 0.05


def test_a_held_disturbance_survives_a_correction_unchanged() -> None:
    """With learning held, a reading corrects the temperatures only.

    The disturbance, its variance and its correlation with the temperatures
    stay where they were, so the next learning step starts from them.
    """
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([20.0, 30.0]))
    for k in range(30):
        obs.update(20.0 + 0.01 * k, u=0.3, T_outdoor=5.0, dt_s=300.0)
    rate, variance = obs.disturbance_K_per_min, float(obs.P[2, 2])
    room_before = float(obs.x_hat[0])

    obs.update(22.0, u=0.3, T_outdoor=5.0, dt_s=300.0, learn_disturbance=False)

    assert obs.disturbance_K_per_min == rate
    assert float(obs.P[2, 2]) == variance
    np.testing.assert_array_equal(obs.P[:2, 2], [0.0, 0.0])
    assert float(obs.x_hat[0]) > room_before + 0.5


def _observer_predicting(room_temperature: float) -> tuple[KalmanObserver, float]:
    """Return an observer and the room temperature it predicts one step on."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    obs = _make_observer(plant)
    obs.initialise(np.array([room_temperature, room_temperature]))
    predicted = float(plant.propagate(obs.x_hat, 0.0, room_temperature, 300.0)[0])
    return obs, predicted


def test_a_repeat_centred_on_the_prediction_leaves_the_estimate_there() -> None:
    """A band symmetric about the prediction moves nothing and sharpens less.

    The truncated reading keeps its mean, so the estimate stays on the
    prediction; its variance shrinks, but less than a point reading would
    shrink it.
    """
    obs, predicted = _observer_predicting(20.0)
    point, _ = _observer_predicting(20.0)

    x_hat = obs.update(predicted, u=0.0, T_outdoor=20.0, dt_s=300.0, band_K=0.5)
    point.update(predicted, u=0.0, T_outdoor=20.0, dt_s=300.0)

    assert float(x_hat[0]) == pytest.approx(predicted, abs=1e-12)
    assert float(point.P[0, 0]) < float(obs.P[0, 0])


@pytest.mark.parametrize("side", [1.0, -1.0])
def test_a_repeat_pushes_a_prediction_outside_the_band_back_towards_it(
    side: float,
) -> None:
    """A prediction beyond the band edge moves towards the band, not onto the value.

    The room is known to lie within the band, so a prediction outside it is
    pulled in. A point reading of the same value would pull it much further,
    towards the reported value itself.
    """
    obs, predicted = _observer_predicting(20.0)
    point, _ = _observer_predicting(20.0)
    band_K = 0.2
    reported = predicted - side * 0.5
    edge = reported + side * band_K

    x_band = float(
        obs.update(reported, u=0.0, T_outdoor=20.0, dt_s=300.0, band_K=band_K)[0]
    )
    x_point = float(point.update(reported, u=0.0, T_outdoor=20.0, dt_s=300.0)[0])

    assert side * (predicted - x_band) > 0.0
    assert side * (x_band - edge) > -0.05
    assert side * (x_band - x_point) > 0.05


def test_a_narrow_band_acts_as_a_point_reading() -> None:
    """As the band closes, the repeat becomes the fresh reading it bounds."""
    obs, predicted = _observer_predicting(20.0)
    point, _ = _observer_predicting(20.0)
    reported = predicted + 0.3

    x_band = obs.update(reported, u=0.0, T_outdoor=20.0, dt_s=300.0, band_K=1e-6)
    x_point = point.update(reported, u=0.0, T_outdoor=20.0, dt_s=300.0)

    np.testing.assert_allclose(x_band, x_point, atol=1e-5)
    np.testing.assert_allclose(obs.P, point.P, atol=1e-8)


def test_a_band_far_from_the_prediction_acts_as_a_reading_at_its_edge() -> None:
    """A band in the far tail of the prediction is a reading at the nearer edge.

    The truncated moments lose their precision there, so the band stands in
    for a point reading at the edge closest to the prediction.
    """
    obs, predicted = _observer_predicting(20.0)
    edge_reading, _ = _observer_predicting(20.0)
    reported = predicted + 30.0

    x_band = obs.update(reported, u=0.0, T_outdoor=20.0, dt_s=300.0, band_K=0.5)
    x_edge = edge_reading.update(reported - 0.5, u=0.0, T_outdoor=20.0, dt_s=300.0)

    np.testing.assert_allclose(x_band, x_edge)


def test_a_coarser_sensor_lets_the_disturbance_wander_less() -> None:
    """The disturbance's process noise shrinks with the square of the threshold.

    Predicting through an interval with an uninformative band, the
    disturbance variance grows by the scaled process noise alone.
    """
    growth = {}
    for threshold_K in (0.1, 0.5):
        plant = PlantModelRC2(PlantParams(), dt_s=30.0)
        obs = _make_observer(plant)
        obs.initialise(np.array([20.0, 20.0]))
        before = float(obs.P[2, 2])
        obs.update(
            20.0,
            u=0.0,
            T_outdoor=20.0,
            dt_s=300.0,
            band_K=1e3,
            report_threshold_K=threshold_K,
        )
        growth[threshold_K] = float(obs.P[2, 2]) - before

    assert growth[0.1] == pytest.approx(KalmanParams().q_disturbance * 10.0, rel=1e-6)
    assert growth[0.5] == pytest.approx(growth[0.1] * (0.1 / 0.5) ** 2, rel=1e-6)


def _integrated_moments(
    y_pred: float, s: float, lower: float, upper: float
) -> tuple[float, float]:
    """Return mean shift and variance of ``N(y_pred, s)`` on ``[lower, upper]``.

    Integrates the density numerically on a fine grid, independently of the
    closed form under test.
    """
    grid = np.linspace(lower, upper, 200_001)
    density = np.exp(-0.5 * (grid - y_pred) ** 2 / s)
    mass = np.trapezoid(density, grid)
    mean = np.trapezoid(grid * density, grid) / mass
    variance = np.trapezoid((grid - mean) ** 2 * density, grid) / mass
    return float(mean - y_pred), float(variance)


@pytest.mark.parametrize(
    ("band_centre", "half_width"),
    [
        pytest.param(0.0, 0.3, id="centred"),
        pytest.param(0.25, 0.3, id="above-overlapping"),
        pytest.param(-0.25, 0.3, id="below-overlapping"),
        pytest.param(1.0, 0.2, id="above-tail"),
        pytest.param(-1.0, 0.2, id="below-tail"),
        pytest.param(0.0, 0.02, id="narrow"),
        pytest.param(2.0, 0.25, id="far-above"),
        pytest.param(-2.0, 0.25, id="far-below"),
        pytest.param(2.372, 0.158, id="seven-sigma-above"),
        pytest.param(-2.372, 0.158, id="seven-sigma-below"),
    ],
)
def test_the_band_moments_match_the_truncated_normal(
    band_centre: float, half_width: float
) -> None:
    """The closed-form shift and variance equal the numerically integrated ones.

    The prediction ``N(0, 0.1)`` is truncated to a band whose centre lies
    ``band_centre`` away, on either side and out into either tail. Seven standard
    deviations out, a difference of two distribution values near 1 would
    have cancelled most of the band's probability.
    """
    s = 0.1
    shift, variance = KalmanObserver._reading_moments(band_centre, 0.0, s, half_width)

    ref_shift, ref_variance = _integrated_moments(
        0.0, s, band_centre - half_width, band_centre + half_width
    )
    assert shift == pytest.approx(ref_shift, rel=1e-6, abs=1e-9)
    assert variance == pytest.approx(ref_variance, rel=1e-5, abs=1e-12)


@pytest.mark.parametrize("side", [1.0, -1.0])
def test_a_band_far_from_the_prediction_reads_at_its_nearer_edge(side: float) -> None:
    """On either side, a band in the far tail stands for its nearer edge."""
    shift, variance = KalmanObserver._reading_moments(side * 30.0, 0.0, 0.01, 0.5)

    assert shift == pytest.approx(side * 29.5)
    assert variance == 0.0
