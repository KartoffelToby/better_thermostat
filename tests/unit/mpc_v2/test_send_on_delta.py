"""MPC v2 under a room sensor that reports only on a change (send-on-delta)."""

from __future__ import annotations

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    SNAPSHOT_VERSION,
    ControllerSnapshot,
    MpcV2Controller,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals._types import (
    FloatArray,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
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


_SETPOINT = 21.0
_ROOM_STEP_S = 30.0
_HEARTBEAT_S = 3600.0


def _closed_loop_on_a_coarse_sensor(
    room: PlantParams, threshold_K: float, hours: float = 24.0
) -> tuple[float, float]:
    """Run MPC v2 against a room read through a send-on-delta sensor.

    The sensor rounds to 0.1 K and reports when its value has moved by
    ``threshold_K`` since the last report, or once an hour. Each five-minute
    cycle hands MPC v2 the last report. Returns the valve travel (sum of the
    percent changes) and the largest deviation of the room from the
    setpoint, both over the run after its first quarter.
    """
    plant = PlantModelRC2(room, dt_s=_ROOM_STEP_S)
    x = np.array([_SETPOINT, _SETPOINT + 10.0])
    reported, reported_at = round(_SETPOINT, 1), 0.0
    state: MpcV2State | None = None
    applied: int | None = None
    valves: list[int] = []
    rooms: list[float] = []
    t_s = 0.0
    while t_s < hours * 3600.0:
        out, state = compute_mpc_v2(
            MpcV2Input(
                key="send-on-delta",
                target_temperature=_SETPOINT,
                room_temperature=reported,
                trv_temperature=float(x[1]),
                outdoor_temperature=0.0,
                applied_valve_percent=None if applied is None else float(applied),
            ),
            MpcV2Params(),
            state,
            now=t_s,
        )
        assert out is not None
        applied = out.valve_percent
        for _ in range(int(_CYCLE_S / _ROOM_STEP_S)):
            x = plant.discrete_step(x, applied / 100.0, 0.0)
            t_s += _ROOM_STEP_S
            value = round(float(x[0]), 1)
            if (
                abs(value - reported) >= threshold_K - 1e-9
                or t_s - reported_at >= _HEARTBEAT_S
            ):
                reported, reported_at = value, t_s
        valves.append(applied)
        rooms.append(float(x[0]))
    settled = len(valves) // 4
    travel = float(
        sum(abs(b - a) for a, b in zip(valves[settled:], valves[settled + 1 :]))
    )
    deviation = max(abs(r - _SETPOINT) for r in rooms[settled:])
    return travel, deviation


@pytest.mark.parametrize(
    "room",
    [
        pytest.param(PlantParams(), id="room-as-modelled"),
        pytest.param(PlantParams(tau_room_min=640.0), id="slower-room"),
        pytest.param(PlantParams(tau_room_min=240.0), id="faster-room"),
        pytest.param(PlantParams(gain_heater=1.6), id="weaker-radiator"),
    ],
)
def test_a_coarse_sensor_does_not_set_the_valve_swinging(room: PlantParams) -> None:
    """With a 0.5 K sensor the valve settles instead of cycling between reports.

    Taking every repeat as a fresh reading drives a limit cycle: the frozen
    value tells the filter the room holds still, the next report jumps by the
    threshold, and the plan swings the valve from one rail towards the other
    each time, one to two thousand percent of travel over the 18 hours measured
    here. In a room faster or weaker than the model each report also corrects
    the estimate by about the threshold; the weight against valve moves keeps
    the valve from answering that with a jump. The 0.1 K run of the same room
    bounds what a settled valve moves.
    """
    fine_travel, fine_deviation = _closed_loop_on_a_coarse_sensor(room, 0.1)
    travel, deviation = _closed_loop_on_a_coarse_sensor(room, 0.5)

    assert fine_travel < 150.0
    assert fine_deviation < 0.1
    assert travel < 300.0, f"valve travelled {travel:.0f} %"
    assert deviation < 0.25, f"room {deviation:.2f} K off the setpoint"


def test_the_first_reading_after_a_window_pause_adds_no_step() -> None:
    """While the window is open the room moves on; that move is no sensor step.

    The window closes again within the pause limit of the disturbance, so
    only the open window itself keeps the drop out of the steps.
    """
    state: MpcV2State | None = None
    t_s = 0.0
    for k in range(8):
        _, state = compute_mpc_v2(
            MpcV2Input(
                key="window",
                target_temperature=21.0,
                room_temperature=21.0 + (0.5 if k % 2 else 0.0),
                outdoor_temperature=5.0,
            ),
            MpcV2Params(),
            state,
            now=t_s,
        )
        t_s += _CYCLE_S
    assert state is not None and state.controller is not None
    steps = state.controller.report_band.steps

    _, state = compute_mpc_v2(
        MpcV2Input(
            key="window",
            target_temperature=21.0,
            room_temperature=20.0,
            outdoor_temperature=5.0,
            window_open=True,
        ),
        MpcV2Params(),
        state,
        now=t_s,
    )
    _, state = compute_mpc_v2(
        MpcV2Input(
            key="window",
            target_temperature=21.0,
            room_temperature=18.7,
            outdoor_temperature=5.0,
        ),
        MpcV2Params(),
        state,
        now=t_s + 100.0,
    )

    assert state.controller is not None
    assert 100.0 + _CYCLE_S <= state.controller.params.dob.max_reading_interval_s
    assert state.controller.report_band.steps == steps
    assert state.controller.report_band.last_reading == 18.7


def test_a_reading_the_trvs_substitute_reaches_the_controller_as_such() -> None:
    """``compute_mpc_v2`` hands the substitute flag on to the controller."""
    state: MpcV2State | None = None
    for k, from_trvs in enumerate((False, True)):
        _, state = compute_mpc_v2(
            MpcV2Input(
                key="fallback",
                target_temperature=21.0,
                room_temperature=20.0,
                outdoor_temperature=5.0,
                room_temperature_from_trvs=from_trvs,
            ),
            MpcV2Params(),
            state,
            now=k * _CYCLE_S,
        )
        assert state.controller is not None
        expected = None if from_trvs else 20.0
        assert state.controller.report_band.last_reading == expected


def test_readings_the_trvs_substitute_are_fresh_and_teach_nothing() -> None:
    """The TRVs' mean stands in for a failed room sensor without touching the band.

    Each of its readings counts as fresh, even a repeat, and none adds a
    step. The room sensor's first reading afterwards adds none either.
    """
    controller, t_s = _controller_with_steps(0.5)
    steps = controller.report_band.steps
    for reading in (19.4, 19.4, 19.9, 19.9):
        controller.step(
            t_s=t_s,
            T_room=reading,
            T_target=21.0,
            T_outdoor=5.0,
            room_reading_from_trvs=True,
        )
        t_s += _CYCLE_S
        assert controller.report_band.last_reading is None

    controller.step(t_s=t_s, T_room=21.3, T_target=21.0, T_outdoor=5.0)

    assert controller.report_band.steps == steps
    assert controller.report_band.last_reading == 21.3


def test_the_plan_weighs_valve_moves_by_the_learned_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The controller hands the report band's factor to the optimiser."""
    state: MpcV2State | None = None
    for k in range(8):
        _, state = compute_mpc_v2(
            MpcV2Input(
                key="move-suppression",
                target_temperature=21.0,
                room_temperature=21.0 + (0.5 if k % 2 else 0.0),
                outdoor_temperature=5.0,
            ),
            MpcV2Params(),
            state,
            now=k * _CYCLE_S,
        )
    assert state is not None and state.controller is not None
    controller = state.controller
    assert controller.report_band.move_suppression_factor == pytest.approx(25.0)
    seen: list[float] = []
    solve = controller.optimiser.solve

    def spy(
        *,
        x_pred: FloatArray,
        T_sp: float,
        T_outdoor: float,
        u_last: float,
        D_hat_K_per_min: float,
        u_max: float,
        move_suppression_factor: float,
    ) -> float:
        seen.append(move_suppression_factor)
        return solve(
            x_pred=x_pred,
            T_sp=T_sp,
            T_outdoor=T_outdoor,
            u_last=u_last,
            D_hat_K_per_min=D_hat_K_per_min,
            u_max=u_max,
            move_suppression_factor=move_suppression_factor,
        )

    monkeypatch.setattr(controller.optimiser, "solve", spy)
    compute_mpc_v2(
        MpcV2Input(
            key="move-suppression",
            target_temperature=21.0,
            room_temperature=21.0,
            outdoor_temperature=5.0,
        ),
        MpcV2Params(),
        state,
        now=8 * _CYCLE_S,
    )

    assert seen == [pytest.approx(25.0)]
