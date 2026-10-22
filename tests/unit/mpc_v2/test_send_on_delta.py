"""MPC v2 under a room sensor that reports only on a change (send-on-delta)."""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import MpcV2Params
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    SNAPSHOT_VERSION,
    ControllerSnapshot,
    MpcV2Controller,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.report_band import (
    MIN_REPORT_THRESHOLD_K,
)

_CYCLE_S = 300.0


def _controller_with_steps(
    step_K: float, t_s: float = 0.0
) -> tuple[MpcV2Controller, float]:
    """Return a controller that has seen readings in steps of ``step_K``.

    The readings alternate around 21 °C at the setpoint, so the controller
    settles while it learns the sensor's threshold. Returns it with the time
    of the next cycle.
    """
    controller = MpcV2Controller(MpcV2Params())
    for k in range(12):
        reading = 21.0 + (step_K if k % 2 else 0.0)
        controller.step(t_s=t_s, T_room=reading, T_target=21.0, T_outdoor=5.0)
        t_s += _CYCLE_S
    return controller, t_s


def test_a_frozen_coarse_reading_teaches_the_disturbance_less_than_a_fine_one() -> None:
    """A repeat from a coarse sensor is no evidence that the room holds still.

    With the valve shut on a cold day the model predicts the room to cool.
    The sensor keeps reporting the same value. A sensor that reports every
    0.1 K would have reported a cooling room, so its silence means the room
    holds and the filter learns a warming disturbance. A 0.5 K sensor stays
    silent while the room cools by less than that, so the filter learns far
    less from the same silence.
    """
    learned = {}
    for step_K in (0.1, 0.5):
        controller, t_s = _controller_with_steps(step_K)
        assert controller.report_band.threshold_K == pytest.approx(step_K)
        before = controller.kalman.disturbance_K_per_min
        for _ in range(12):
            controller.set_applied_u(0.0)
            controller.step(t_s=t_s, T_room=21.0, T_target=21.0, T_outdoor=-10.0)
            t_s += _CYCLE_S
        learned[step_K] = controller.kalman.disturbance_K_per_min - before

    assert learned[0.1] > 0.0
    assert abs(learned[0.5]) < 0.5 * learned[0.1]


def test_learning_held_keeps_the_threshold_and_the_disturbance() -> None:
    """While learning is paused, readings neither teach the threshold nor the load."""
    controller, t_s = _controller_with_steps(0.5)
    steps = controller.report_band.steps
    rate = controller.kalman.disturbance_K_per_min

    for k in range(6):
        controller.step(
            t_s=t_s,
            T_room=19.0 - 0.3 * k,
            T_target=21.0,
            T_outdoor=5.0,
            learning_paused=True,
        )
        t_s += _CYCLE_S

    assert controller.report_band.steps == steps
    assert controller.kalman.disturbance_K_per_min == rate
    assert controller.report_band.last_reading == pytest.approx(17.5)


def test_a_reading_after_a_pause_corrects_the_room_but_not_the_load() -> None:
    """The jump that closes a pause of the control loop is no standing load."""
    controller, t_s = _controller_with_steps(0.1)
    rate = controller.kalman.disturbance_K_per_min
    steps = controller.report_band.steps

    _, diag = controller.step(
        t_s=t_s + 3600.0, T_room=18.0, T_target=21.0, T_outdoor=5.0
    )

    assert controller.kalman.disturbance_K_per_min == rate
    assert controller.report_band.steps == steps
    assert diag.T_room_hat < 18.5


def test_the_snapshot_carries_the_learned_threshold_across_a_restart() -> None:
    """A restored controller treats the next repeat with the learned band."""
    controller, _ = _controller_with_steps(0.5)
    raw = controller.export_snapshot().to_mapping()

    snap = ControllerSnapshot.from_mapping(raw)
    assert snap is not None
    restored = MpcV2Controller(MpcV2Params())
    restored.restore_snapshot(snap)

    assert restored.report_band.steps == controller.report_band.steps
    assert restored.report_band.last_reading == controller.report_band.last_reading
    assert restored.report_band.threshold_K == pytest.approx(0.5)
    assert restored.kalman.disturbance_K_per_min == pytest.approx(
        controller.kalman.disturbance_K_per_min
    )
    np.testing.assert_allclose(restored.kalman.P, controller.kalman.P)


def test_a_snapshot_from_before_the_disturbance_state_still_restores() -> None:
    """A version 3 snapshot holds a 2×2 covariance and no reading history.

    The temperatures keep their stored covariance, the disturbance takes the
    stored rate, and the threshold starts at its lower bound.
    """
    snap = ControllerSnapshot.from_mapping(
        {
            "v": 3,
            "x_hat": [20.5, 35.0],
            "kalman_P": [[0.02, 0.01], [0.01, 0.5]],
            "D_hat_K_per_min": 0.004,
            "last_u": 0.3,
        }
    )
    assert snap is not None
    assert SNAPSHOT_VERSION > 3
    controller = MpcV2Controller(MpcV2Params())

    controller.restore_snapshot(snap)

    np.testing.assert_allclose(controller.kalman.P[:2, :2], [[0.02, 0.01], [0.01, 0.5]])
    assert controller.kalman.disturbance_K_per_min == pytest.approx(0.004)
    assert controller.report_band.last_reading is None
    assert controller.report_band.threshold_K == MIN_REPORT_THRESHOLD_K


@pytest.mark.parametrize(
    "bogus",
    [
        pytest.param({"room_report_steps": "0.5"}, id="steps-not-a-list"),
        pytest.param({"room_report_steps": [0.5, "x"]}, id="step-not-a-number"),
        pytest.param({"room_report_steps": [float("nan")]}, id="step-not-finite"),
        pytest.param({"last_room_reading": float("inf")}, id="reading-not-finite"),
    ],
)
def test_an_unusable_reading_history_drops_the_snapshot(
    bogus: dict[str, object],
) -> None:
    """A reading history that is not finite numbers poisons nothing."""
    assert ControllerSnapshot.from_mapping({"v": SNAPSHOT_VERSION, **bogus}) is None


def test_a_frozen_coarse_reading_lets_the_estimate_follow_the_model() -> None:
    """Inside the band the room estimate follows the model, not the old value.

    With the valve shut on a cold day the model predicts the room to cool. A
    0.5 K sensor stays silent until the room has moved that far, so the
    estimate cools with the model while it stays within the band, instead of
    sitting on the value as a fresh reading would hold it.
    """
    controller, t_s = _controller_with_steps(0.5)
    estimates = []
    for _ in range(6):
        controller.set_applied_u(0.0)
        _, diag = controller.step(t_s=t_s, T_room=21.0, T_target=15.0, T_outdoor=-10.0)
        estimates.append(diag.T_room_hat)
        t_s += _CYCLE_S

    assert estimates == sorted(estimates, reverse=True)
    assert 21.0 - 0.5 < estimates[-1] < 21.0 - 0.15


def test_a_restored_disturbance_survives_a_snapshot_without_an_estimate() -> None:
    """A snapshot that lost its estimate still carries its disturbance.

    The first cycle seeds the estimate from the reading and keeps the stored
    rate, rather than starting the load from zero.
    """
    snap = ControllerSnapshot.from_mapping(
        {"v": SNAPSHOT_VERSION, "x_hat": [], "D_hat_K_per_min": 0.01}
    )
    assert snap is not None
    controller = MpcV2Controller(MpcV2Params())
    controller.restore_snapshot(snap)
    assert not controller.initialised

    controller.step(t_s=0.0, T_room=21.0, T_target=21.0, T_outdoor=5.0)

    assert controller.kalman.disturbance_K_per_min == pytest.approx(0.01, rel=0.2)
