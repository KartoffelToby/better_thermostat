"""MPC v2 on a plant prior faster than the room it heats.

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

The same fast prior predicts the room's response to a setpoint step far
faster than it comes. While the room warms up, that shows as a load the
estimate could absorb; it may not, or the plan overheats the room after the
step. The estimate takes the wider bound only once the room has settled.
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


STEP_OUTDOOR = 5.0
STEP_SCHEDULE_H = ((0.0, 20.0), (1.0, 22.0), (3.0, 21.0))
STEP_HOURS = 7.0
# What the rate bound alone holds the 60 min prior to after each step.
STEP_UP_OVERSHOOT_BOUND_K = 0.1
STEP_DOWN_OVERSHOOT_BOUND_K = 1.1
LATE_SHORTFALL_BOUND_K = 0.25


def _setpoint_at(hour: float) -> float:
    return [sp for start, sp in STEP_SCHEDULE_H if hour >= start][-1]


def test_setpoint_steps_on_a_fast_prior_keep_the_narrow_bound() -> None:
    """Setpoint steps on a prior far faster than the room do not overheat it.

    The prior is the fastest AUTO derives, against the default room. 20 → 22 °C at hour 1 and back to 21 °C at hour 3, outdoor sensor present.
    The room may overshoot 22 °C by at most 0.1 K, 21 °C by at most 1.1 K
    after the drop (the radiator still holds its heat), and from hour 5 on may
    sit at most 0.25 K below 21 °C. A disturbance bound that widens during
    the warm-up overshoots 22 °C by 0.55 K and 21 °C by 1.6 K, and leaves the
    room 0.4 K short afterwards.
    """
    room = PlantModelRC2(PlantParams(), dt_s=ROOM_STEP_S)
    params = MpcV2Params(
        plant=replace(PlantParams(), tau_room_min=TAU_ROOM_BOUNDS_MIN[0])
    )
    x = np.array([20.0, 20.0 + (20.0 - STEP_OUTDOOR)])
    state: MpcV2State | None = None
    applied: int | None = None
    samples: list[tuple[float, float, float]] = []
    t_s = 0.0
    for _ in range(int(STEP_HOURS * 3600.0 / CYCLE_S)):
        hour = t_s / 3600.0
        out, state = compute_mpc_v2(
            MpcV2Input(
                key="setpoint-steps",
                target_temperature=_setpoint_at(hour),
                room_temperature=float(x[0]),
                trv_temperature=float(x[1]),
                outdoor_temperature=STEP_OUTDOOR,
                applied_valve_percent=None if applied is None else float(applied),
            ),
            params,
            state,
            now=1_700_000_000.0 + t_s,
        )
        assert out is not None
        applied = out.valve_percent
        assert state.controller is not None
        samples.append((hour, float(x[0]), state.controller.dob.D_hat_K_per_min))
        for _step in range(int(CYCLE_S / ROOM_STEP_S)):
            x = room.discrete_step(x, applied / 100.0, STEP_OUTDOOR)
            t_s += ROOM_STEP_S

    up = max(room_c - 22.0 for hour, room_c, _ in samples if 1.0 <= hour < 3.0)
    down = max(room_c - 21.0 for hour, room_c, _ in samples if hour >= 3.0)
    late = max(21.0 - room_c for hour, room_c, _ in samples if hour >= 5.0)
    # The warm-up has to press the estimate against the rate bound, or the
    # bound was never tested.
    assert min(d for _, _, d in samples) == pytest.approx(-0.05)
    assert up <= STEP_UP_OVERSHOOT_BOUND_K
    assert down <= STEP_DOWN_OVERSHOOT_BOUND_K
    assert late <= LATE_SHORTFALL_BOUND_K
