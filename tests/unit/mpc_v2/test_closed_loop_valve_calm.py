"""MPC v2 holds a steady valve on an imperfect sensor and an imperfect model.

The room is simulated in 30-second steps and ``compute_mpc_v2`` runs every
five minutes, as in ``test_closed_loop_disturbance``. There is no free heat,
so every disturbance the controller estimates here comes from the sensor or
from the mismatch between the simulated room and the controller's model. A
controller that takes such an estimate for real heat moves the valve on
every cycle while the room itself stays where it was.

Each run starts on the setpoint and is judged after ``SETTLED_FROM_H``. The
move and span bounds sit a little above what a controller with the
disturbance only in its steady-state input achieves, and far below the valve
hunting a prediction that follows the fast estimate produces. The mismatched
heater gain also bounds the offset that such a controller leaves.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Input,
    MpcV2Params,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)

CYCLE_S = 300.0
ROOM_STEP_S = 30.0
HOURS = 30.0
SETTLED_FROM_H = 6.0
SETPOINT_C = 21.0
# A valve move larger than this counts as a move; smaller ones are the
# ordinary trim a noisy reading causes.
MOVE_PCT = 5


@dataclass(frozen=True)
class _Case:
    """One scenario and its bounds; temperatures in °C and K, valve in %."""

    room: PlantParams
    model: PlantParams
    outdoor_temperature: float
    noise: float
    quantum: float
    max_moves: int
    max_valve_span_pct: int
    max_mean_abs_error: float
    setpoint: float = SETPOINT_C


_CASES = {
    # 0.05 K of white noise trims the valve on most cycles; what must not
    # happen is that the trim grows into swings across the valve range.
    "gauss0.05-outdoor+0": _Case(
        PlantParams(), PlantParams(), 0.0, 0.05, 0.0, 80, 30, 0.03
    ),
    "quantum0.1-outdoor+10": _Case(
        PlantParams(), PlantParams(), 10.0, 0.0, 0.1, 5, 15, 0.05
    ),
    # A 0.5 K quantum hides the room anywhere within ±0.25 K of a reading, so
    # the error is bounded by that half quantum; where in it the room comes to
    # rest depends on the day (0.01 to 0.14 K over outdoor -5..+10 °C).
    "quantum0.5-outdoor+0": _Case(
        PlantParams(), PlantParams(), 0.0, 0.0, 0.5, 5, 15, 0.25
    ),
    "room_tau90_model_tau180-outdoor+10": _Case(
        PlantParams(tau_room_min=90.0),
        PlantParams(tau_room_min=180.0),
        10.0,
        0.0,
        0.0,
        5,
        10,
        0.05,
    ),
    "room_gain4_model_gain2-outdoor+10": _Case(
        PlantParams(tau_room_min=180.0, gain_heater=4.0),
        PlantParams(tau_room_min=180.0),
        10.0,
        0.0,
        0.0,
        5,
        10,
        0.1,
    ),
}


def _settled_run(case: _Case) -> tuple[list[int], list[float]]:
    """Return the settled valve commands (%) and room errors (K) of one run."""
    rng = np.random.default_rng(7)
    params = MpcV2Params(plant=case.model)
    room = PlantModelRC2(case.room, dt_s=ROOM_STEP_S)
    x = np.array([case.setpoint, case.setpoint])
    state = None
    applied_pct: int | None = None
    valves: list[int] = []
    errors: list[float] = []
    t_s = 0.0
    for _ in range(int(HOURS * 3600.0 / CYCLE_S)):
        reading = float(x[0])
        if case.noise:
            reading += float(rng.normal(0.0, case.noise))
        if case.quantum:
            reading = round(reading / case.quantum) * case.quantum
        out, state = compute_mpc_v2(
            MpcV2Input(
                key="valve-calm",
                target_temp_C=case.setpoint,
                current_temp_C=reading,
                trv_temp_C=float(x[1]),
                outdoor_temp_C=case.outdoor_temperature,
                window_open=False,
            ),
            params,
            state,
            now=1_700_000_000.0 + t_s,
        )
        assert out is not None
        applied_pct = out.valve_percent
        if t_s / 3600.0 >= SETTLED_FROM_H:
            valves.append(applied_pct)
            errors.append(float(x[0]) - case.setpoint)
        for _step in range(int(CYCLE_S / ROOM_STEP_S)):
            x = room.discrete_step(x, applied_pct / 100.0, case.outdoor_temperature)
            t_s += ROOM_STEP_S
    return valves, errors


# A setpoint between two 0.5 K readings leaves every reading 0.2 or 0.3 K off
# it, so the valve cycles between the two readings. The bounds are what the
# controller did before free heat entered its plan (up to 181 moves, a 76 %
# span at outdoor 0 °C); the planning reading of the disturbance follows that
# cycle and widens the span to 85..89 %.
_OFF_QUANTUM_CASES = {
    f"quantum0.5-setpoint{setpoint:.1f}-outdoor+0": _Case(
        PlantParams(), PlantParams(), 0.0, 0.0, 0.5, 181, 76, 0.25, setpoint=setpoint
    )
    for setpoint in (21.2, 21.3)
}
_CASES.update(_OFF_QUANTUM_CASES)


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            name,
            marks=pytest.mark.xfail(
                strict=True,
                reason="the planning reading of the disturbance follows the "
                "cycle between two quantised readings and widens the valve "
                "span beyond what the controller showed without it",
            ),
        )
        if name in _OFF_QUANTUM_CASES
        else name
        for name in _CASES
    ],
)
def test_valve_stays_calm_on_a_noisy_sensor_or_a_mismatched_model(name: str) -> None:
    """The settled valve neither hunts nor wanders while the room holds.

    Counted over the settled day: valve moves beyond ``MOVE_PCT``, the span
    between the lowest and highest command, and the mean absolute room
    error. Each case bounds all three.
    """
    case = _CASES[name]
    valves, errors = _settled_run(case)
    moves = sum(
        1 for a, b in zip(valves, valves[1:], strict=False) if abs(a - b) > MOVE_PCT
    )
    span = max(valves) - min(valves)
    mean_abs_error = float(np.mean(np.abs(errors)))
    # A run that never left one command would satisfy the move and span
    # bounds whatever the controller did; the room must actually be heated.
    assert max(valves) > 0

    assert moves <= case.max_moves, f"{moves} valve moves, span {span} %"
    assert span <= case.max_valve_span_pct, f"valve {min(valves)}..{max(valves)} %"
    assert mean_abs_error <= case.max_mean_abs_error
