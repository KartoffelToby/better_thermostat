"""MPC v2 on a fast plant prior in a room without an outdoor sensor.

Without an outdoor sensor the controller assumes ``OUTDOOR_TEMPERATURE_FALLBACK``
(10 °C). On a -5 °C day the room loses 15 K more than the model expects, and
the disturbance estimate has to carry that error. A rate ``D`` stands for
``D·τ_room`` of heat balance, so on a fast prior the estimate needs a larger
rate for the same error: 15 K / 180 min = 0.083 K/min on the small-room
preset, 0.25 K/min at the shortest ``τ_room`` the AUTO prior derives.

The simulated room is the default RC2 room (``τ_room`` 480 min) and reaches
the setpoint with the valve about 72 % open, so any settled shortfall belongs
to the controller. ``compute_mpc_v2`` runs every five minutes against the room
integrated in 30-second steps.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    PLANT_PRESETS,
    MpcV2Input,
    MpcV2Params,
    MpcV2PlantPreset,
    MpcV2State,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    TAU_ROOM_BOUNDS_MIN,
    PlantModelRC2,
    PlantParams,
)

CYCLE_S = 300.0
ROOM_STEP_S = 30.0
HOURS = 36.0
SETTLED_FROM_H = 28.0
SETPOINT = 21.0
OUTDOOR = -5.0
MEAN_ERROR_BOUND_K = 0.1


def _settled_run(prior: PlantParams) -> tuple[float, list[int]]:
    """Return the settled mean room error and the settled valve commands."""
    room = PlantModelRC2(PlantParams(), dt_s=ROOM_STEP_S)
    params = MpcV2Params(plant=prior)
    x = np.array([SETPOINT, SETPOINT])
    state: MpcV2State | None = None
    applied: int | None = None
    errors: list[float] = []
    valves: list[int] = []
    t_s = 0.0
    for _ in range(int(HOURS * 3600.0 / CYCLE_S)):
        out, state = compute_mpc_v2(
            MpcV2Input(
                key="model-mismatch",
                target_temperature=SETPOINT,
                room_temperature=float(x[0]),
                trv_temperature=float(x[1]),
                outdoor_temperature=None,
                applied_valve_percent=None if applied is None else float(applied),
            ),
            params,
            state,
            now=1_700_000_000.0 + t_s,
        )
        assert out is not None
        applied = out.valve_percent
        if t_s >= SETTLED_FROM_H * 3600.0:
            errors.append(float(x[0]) - SETPOINT)
            valves.append(applied)
        for _step in range(int(CYCLE_S / ROOM_STEP_S)):
            x = room.discrete_step(x, applied / 100.0, OUTDOOR)
            t_s += ROOM_STEP_S
    return float(np.mean(errors)), valves


@pytest.mark.parametrize(
    "prior",
    [
        pytest.param(
            replace(PLANT_PRESETS[MpcV2PlantPreset.SMALL_ROOM]), id="small_room"
        ),
        pytest.param(
            replace(PlantParams(), tau_room_min=TAU_ROOM_BOUNDS_MIN[0]),
            id="auto_fastest",
        ),
    ],
)
def test_missing_outdoor_sensor_on_a_fast_prior_holds_the_setpoint(
    prior: PlantParams,
) -> None:
    """The settled room sits on the setpoint while the valve has headroom.

    The valve has to settle well inside its range, or a shortfall would be
    physical rather than the controller's.
    """
    mean_error, valves = _settled_run(prior)

    assert len(valves) == int((HOURS - SETTLED_FROM_H) * 3600.0 / CYCLE_S)
    assert max(valves) < 90
    assert abs(mean_error) <= MEAN_ERROR_BOUND_K, (
        f"settled error {mean_error:+.3f} K with the valve at "
        f"{min(valves)}..{max(valves)} %"
    )
