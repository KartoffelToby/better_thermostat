"""Closed-loop behaviour of MPC v2 under free heat, setpoint steps and airing.

``compute_mpc_v2`` runs once every five minutes, the usual Home Assistant
sensor cadence, against an RC2 room integrated in 30-second steps. The
simulated room and the controller share one set of plant parameters, so any
error that remains belongs to the controller and not to model mismatch.

Free heat (sun, a second heat source) enters the room equation as a constant
rate in K/min. That is the term the controller's disturbance estimate stands
for: ``steady_radiator_temp`` subtracts ``D·tau_room`` from the room's heat
loss, which is exactly what a constant ``D`` in ``dT_room/dt`` does to the
fixed point.

The axis is the cross product of free heat {0, 0.02, 0.03} K/min, outdoor
temperature {0, -10, -16} °C and the event the room recovers from: a setpoint
step or a ventilation gap. Where the default radiator cannot reach the
setpoint without free heat, the requirement is a fully open valve instead of
a small tracking error.

Two requirements sit underneath the loop: the disturbance estimate has to
match the free heat that is present, and the radiator estimate has to stay
physical across a long gap between room readings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    PLANT_PRESETS,
    MpcV2Controller,
    MpcV2Input,
    MpcV2Params,
    MpcV2State,
    compute_mpc_v2,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)

CYCLE_S = 300.0
ROOM_STEP_S = 30.0
HOURS = 16.0
SETPOINT_C = 21.0
STEP_FROM_C = 18.0
STEP_AT_H = 4.0
WINDOW_OPEN_H = (6.0, 6.25)
# An open window cools the room by about 1.2 K over the quarter hour.
DRAFT_K_PER_MIN = -0.08
SETTLED_FROM_H = 12.0

MEAN_ERROR_BOUND_K = 0.1
OVERSHOOT_BOUND_K = 0.1

FREE_HEAT_K_PER_MIN = (0.0, 0.02, 0.03)
OUTDOOR_C = (0.0, -10.0, -16.0)


@dataclass(frozen=True)
class _Trace:
    """Per-cycle samples of one simulated run."""

    hours: tuple[float, ...]
    room: tuple[float, ...]
    setpoint: tuple[float, ...]
    valve_percent: tuple[int, ...]
    window_open: tuple[bool, ...]

    def indices_from(self, hour: float) -> list[int]:
        """Return the cycle indices at or after ``hour``."""
        return [i for i, h in enumerate(self.hours) if h >= hour]


def _simulate(
    *,
    plant: PlantParams,
    outdoor: float,
    free_heat_k_per_min: float | Callable[[float], float],
    setpoint_at: Callable[[float], float],
    start: float,
    window_open_h: tuple[float, float] | None = None,
) -> _Trace:
    """Run ``compute_mpc_v2`` against a simulated room for ``HOURS``.

    The valve command of each cycle is applied until the next one and echoed
    back as the applied position. While the window is open the controller
    returns no command and the valve stays closed, as Better Thermostat does.
    """
    params = MpcV2Params(plant=plant)
    room = PlantModelRC2(plant, dt_s=ROOM_STEP_S)
    x = np.array([start, start])
    state: MpcV2State | None = None
    applied_pct: int | None = None
    hours: list[float] = []
    rooms: list[float] = []
    setpoints: list[float] = []
    valves: list[int] = []
    windows: list[bool] = []
    t_s = 0.0
    for _ in range(int(HOURS * 3600.0 / CYCLE_S)):
        hour = t_s / 3600.0
        is_open = (
            window_open_h is not None and window_open_h[0] <= hour < window_open_h[1]
        )
        setpoint = setpoint_at(hour)
        out, state = compute_mpc_v2(
            MpcV2Input(
                key="closed-loop",
                target_temp_C=setpoint,
                current_temp_C=float(x[0]),
                trv_temp_C=float(x[1]),
                outdoor_temp_C=outdoor,
                window_open=is_open,
            ),
            params,
            state,
            now=1_700_000_000.0 + t_s,
        )
        if is_open:
            assert out is None
            applied_pct = 0
        else:
            assert out is not None
            applied_pct = out.valve_percent
        hours.append(hour)
        rooms.append(float(x[0]))
        setpoints.append(setpoint)
        valves.append(applied_pct)
        windows.append(is_open)
        draft = DRAFT_K_PER_MIN if is_open else 0.0
        for _step in range(int(CYCLE_S / ROOM_STEP_S)):
            free_heat = (
                free_heat_k_per_min(t_s / 3600.0)
                if callable(free_heat_k_per_min)
                else free_heat_k_per_min
            )
            x = room.discrete_step(x, applied_pct / 100.0, outdoor, free_heat + draft)
            t_s += ROOM_STEP_S
    return _Trace(
        tuple(hours), tuple(rooms), tuple(setpoints), tuple(valves), tuple(windows)
    )


def _ceiling(plant: PlantParams, outdoor: float, free_heat: float) -> float:
    """Return the room temperature a fully open valve settles at.

    Solves both RC2 balances at ``u = 1``: the radiator delivers
    ``gain·(T_water − T_rad)`` and loses ``T_rad − T_room``, which the room
    passes on as ``coupling·(T_rad − T_room) = T_room − T_out − D·tau_room``.
    """
    g, c, water = plant.gain_heater, plant.coupling_rad_room, plant.T_water_C
    k = (g + 1.0) / c
    return (g * water + k * (outdoor + free_heat * plant.tau_room_min)) / (g + k)


def _start_temp(outdoor: float, free_heat: float, setpoint: float) -> float:
    """Return a start temperature the room can hold at ``setpoint``."""
    return min(setpoint, _ceiling(PlantParams(), outdoor, free_heat) - 0.5)


@cache
def _step_run(outdoor: float, free_heat: float) -> _Trace:
    """Settle on a lower setpoint, then step up to ``SETPOINT_C``."""
    return _simulate(
        plant=PlantParams(),
        outdoor=outdoor,
        free_heat_k_per_min=free_heat,
        setpoint_at=lambda h: STEP_FROM_C if h < STEP_AT_H else SETPOINT_C,
        start=_start_temp(outdoor, free_heat, STEP_FROM_C),
    )


@cache
def _gap_run(outdoor: float, free_heat: float) -> _Trace:
    """Hold ``SETPOINT_C`` with a quarter hour of open window in between."""
    return _simulate(
        plant=PlantParams(),
        outdoor=outdoor,
        free_heat_k_per_min=free_heat,
        setpoint_at=lambda h: SETPOINT_C,
        start=_start_temp(outdoor, free_heat, SETPOINT_C),
        window_open_h=WINDOW_OPEN_H,
    )


def _reachable(outdoor: float, free_heat: float) -> bool:
    return _ceiling(PlantParams(), outdoor, free_heat) >= SETPOINT_C + 0.5


def _cells() -> list:
    """Return the outdoor × free-heat grid as test parameters."""
    return [
        pytest.param(
            outdoor, free_heat, id=f"outdoor{outdoor:+.0f}-free_heat{free_heat:.2f}"
        )
        for outdoor in OUTDOOR_C
        for free_heat in FREE_HEAT_K_PER_MIN
    ]


def test_grid_holds_reachable_and_unreachable_cells() -> None:
    """The axis covers both regimes the settled requirement distinguishes.

    Without free heat the default radiator tops out below the setpoint on the
    two cold days, and every cell with free heat can reach it.
    """
    unreachable = {
        (outdoor, free_heat)
        for outdoor in OUTDOOR_C
        for free_heat in FREE_HEAT_K_PER_MIN
        if not _reachable(outdoor, free_heat)
    }
    assert unreachable == {(-10.0, 0.0), (-16.0, 0.0)}
    assert _ceiling(PlantParams(), -10.0, 0.0) == pytest.approx(20.0)
    assert _ceiling(PlantParams(), -16.0, 0.0) == pytest.approx(16.4)


@pytest.mark.parametrize(("outdoor", "free_heat"), _cells())
def test_settled_room_holds_the_setpoint_under_standing_free_heat(
    outdoor: float, free_heat: float
) -> None:
    """Hours after a setpoint step the room sits on the setpoint.

    A reachable setpoint is held with a mean error within
    ``MEAN_ERROR_BOUND_K``, whatever part of the heat comes from outside the
    radiator. An unreachable one keeps the valve fully open on every settled
    cycle, with the room within half a kelvin of what an open valve delivers.
    """
    trace = _step_run(outdoor, free_heat)
    settled = trace.indices_from(SETTLED_FROM_H)
    errors = [trace.room[i] - trace.setpoint[i] for i in settled]
    # A frozen or empty window would satisfy every bound below.
    assert len(settled) == int((HOURS - SETTLED_FROM_H) * 3600.0 / CYCLE_S)
    assert len({round(trace.room[i], 6) for i in settled}) > 1

    if _reachable(outdoor, free_heat):
        assert abs(float(np.mean(errors))) <= MEAN_ERROR_BOUND_K, (
            f"mean settled error {np.mean(errors):+.3f} K, valve "
            f"{min(trace.valve_percent[i] for i in settled)}.."
            f"{max(trace.valve_percent[i] for i in settled)} %"
        )
    else:
        assert [trace.valve_percent[i] for i in settled] == [100] * len(settled)
        ceiling = _ceiling(PlantParams(), outdoor, free_heat)
        assert all(abs(trace.room[i] - ceiling) < 0.5 for i in settled)


def _assert_recovery_without_overshoot(
    trace: _Trace, event_end_h: float, outdoor: float, free_heat: float
) -> None:
    """Check the recovery that follows ``event_end_h``.

    A reachable setpoint is recovered with the valve at its upper rail for
    at least one cycle, the room climbing through distinct temperatures for
    the first two hours, and never more than ``OVERSHOOT_BOUND_K`` above the
    setpoint afterwards. An unreachable one leaves nothing to overshoot: the
    valve reaches its rail within the rate limit and stays there.
    """
    after = trace.indices_from(event_end_h)
    valves = [trace.valve_percent[i] for i in after]
    if not _reachable(outdoor, free_heat):
        first_rail = valves.index(100)
        assert first_rail <= 3
        assert valves[first_rail:] == [100] * (len(valves) - first_rail)
        return

    recovery = [i for i in after if trace.hours[i] < event_end_h + 2.0]
    rail_cycles = valves.count(100)
    overshoot = max(trace.room[i] - SETPOINT_C for i in after)
    assert rail_cycles >= 1
    assert len({round(trace.room[i], 2) for i in recovery}) >= len(recovery) // 2
    assert overshoot <= OVERSHOOT_BOUND_K, (
        f"overshoot {overshoot:+.3f} K, valve at the rail for {rail_cycles} cycles"
    )


@pytest.mark.parametrize(("outdoor", "free_heat"), _cells())
def test_setpoint_step_is_reached_without_overshoot(
    outdoor: float, free_heat: float
) -> None:
    """A setpoint raised by 3 K is approached from below and not overshot."""
    trace = _step_run(outdoor, free_heat)
    assert trace.setpoint[trace.indices_from(STEP_AT_H)[0] - 1] == STEP_FROM_C
    _assert_recovery_without_overshoot(trace, STEP_AT_H, outdoor, free_heat)


@pytest.mark.parametrize(("outdoor", "free_heat"), _cells())
def test_ventilation_gap_is_recovered_without_overshoot(
    outdoor: float, free_heat: float
) -> None:
    """After a quarter hour of airing the room returns without overshooting.

    The window cycles return no command and the valve stays closed, which
    costs the room more than half a kelvin before the recovery starts.
    """
    trace = _gap_run(outdoor, free_heat)
    gap_cycles = [i for i, is_open in enumerate(trace.window_open) if is_open]
    after_gap = trace.indices_from(WINDOW_OPEN_H[1])[0]
    assert len(gap_cycles) == round(
        (WINDOW_OPEN_H[1] - WINDOW_OPEN_H[0]) * 3600.0 / CYCLE_S
    )
    assert all(trace.valve_percent[i] == 0 for i in gap_cycles)
    assert trace.room[after_gap] < trace.room[gap_cycles[0]] - 0.5
    _assert_recovery_without_overshoot(trace, WINDOW_OPEN_H[1], outdoor, free_heat)


# A setpoint the large-room radiator reaches only with free heat on a -16 °C
# day: 2·25 + 16 = 66 exceeds the 65 °C water, so the radiator temperature the
# setpoint needs without free heat lies above the water temperature.
HARD_OUTDOOR_C = -16.0
HARD_SETPOINT_C = 25.0


@pytest.mark.parametrize(
    "free_heat",
    [
        pytest.param(free_heat, id=f"free_heat{free_heat:.3f}")
        for free_heat in (0.0, 0.012, 0.02, 0.03)
    ],
)
def test_cold_room_below_a_high_setpoint_keeps_heating(free_heat: float) -> None:
    """A room below its setpoint keeps its valve open.

    With a setpoint at or beyond what the radiator can hold without help,
    free heat decides whether it is reachable. Either way the room must not
    drop below the lower of setpoint and ceiling by more than half a kelvin,
    and no cycle after the first hour may close the valve while the room is
    more than half a kelvin below the setpoint.
    """
    plant = replace(PLANT_PRESETS["large_room"])
    trace = _simulate(
        plant=plant,
        outdoor=HARD_OUTDOOR_C,
        free_heat_k_per_min=free_heat,
        setpoint_at=lambda h: HARD_SETPOINT_C,
        start=22.0,
    )
    target = min(HARD_SETPOINT_C, _ceiling(plant, HARD_OUTDOOR_C, free_heat))
    cold = [i for i in trace.indices_from(1.0) if trace.room[i] < HARD_SETPOINT_C - 0.5]
    closed_while_cold = [
        (round(trace.hours[i], 2), round(trace.room[i], 2))
        for i in cold
        if trace.valve_percent[i] == 0
    ]
    # The requirement only bites on cycles that find the room cold, so the
    # run has to contain a real stretch of them.
    assert len(cold) >= 12

    assert closed_while_cold == [], (
        f"valve closed in {len(closed_while_cold)} cold cycles, first "
        f"{closed_while_cold[:3]}; room ends at {trace.room[-1]:.2f} °C"
    )
    assert trace.room[-1] >= target - 0.5


@pytest.mark.parametrize(
    "cycle_s", [pytest.param(60.0, id="cycle60s"), pytest.param(300.0, id="cycle300s")]
)
@pytest.mark.parametrize(
    "free_heat",
    [
        pytest.param(free_heat, id=f"free_heat{free_heat:.2f}")
        for free_heat in (0.0, 0.02, 0.03)
    ],
)
def test_disturbance_estimate_matches_a_standing_heat_gain(
    free_heat: float, cycle_s: float
) -> None:
    """The disturbance estimate converges on the free heat actually present.

    With the valve held at a fixed opening and a constant free heat in the
    room equation, the estimate the controller feeds forward settles within
    ten percent of that rate (or within 1e-4 K/min of zero without any).
    """
    plant = PlantParams()
    controller = MpcV2Controller(MpcV2Params(plant=plant))
    room = PlantModelRC2(plant, dt_s=ROOM_STEP_S)
    x = np.array([20.0, 20.0])
    t_s = 1_700_000_000.0
    estimates: list[float] = []
    for _ in range(int(24 * 3600.0 / cycle_s)):
        controller.step(
            t_s=t_s,
            T_room_C=float(x[0]),
            T_target_C=21.0,
            T_outdoor_C=0.0,
            T_rad_C=float(x[1]),
        )
        controller.set_applied_u(0.3)
        estimates.append(controller.dob.D_hat_K_per_min)
        for _step in range(int(cycle_s / ROOM_STEP_S)):
            x = room.discrete_step(x, 0.3, 0.0, free_heat)
            t_s += ROOM_STEP_S

    last_hour = estimates[-int(3600.0 / cycle_s) :]
    settled = float(np.mean(last_hour))
    assert max(last_hour) - min(last_hour) < 1e-4
    if free_heat == 0.0:
        assert abs(settled) < 1e-4
    else:
        assert settled == pytest.approx(free_heat, rel=0.1), (
            f"estimate {settled:.4f} K/min for {free_heat} K/min, ratio "
            f"{settled / free_heat:.2f}"
        )


@pytest.mark.parametrize(
    "gap_s",
    [
        pytest.param(gap_s, id=label)
        for gap_s, label in (
            (300.0, "gap5min"),
            (900.0, "gap15min"),
            (3600.0, "gap1h"),
            (86_400.0, "gap1d"),
        )
    ],
)
def test_radiator_estimate_stays_below_the_water_temperature_across_a_gap(
    gap_s: float,
) -> None:
    """The radiator estimate stays at or below the supply water temperature.

    A radiator cannot get hotter than the water that feeds it, whatever the
    valve did and however long the observer has to propagate between two
    room readings.
    """
    water = PlantParams().T_water_C
    controller = MpcV2Controller(MpcV2Params())
    controller.step(t_s=1_000.0, T_room_C=21.0, T_target_C=21.0, T_outdoor_C=5.0)
    controller.set_applied_u(1.0)
    before = float(controller.kalman.x_hat[1])

    _, diag = controller.step(
        t_s=1_000.0 + gap_s, T_room_C=21.0, T_target_C=21.0, T_outdoor_C=5.0
    )

    # The open valve has to heat the estimate noticeably for the bound to bite.
    assert diag.T_rad_hat > before + 10.0
    assert diag.T_rad_hat <= water, (
        f"radiator estimate {diag.T_rad_hat:.1f} °C after {gap_s:.0f} s, water "
        f"{water:.0f} °C"
    )


# A sunny afternoon: the gain ramps up over an hour, holds for four and
# ramps down over another hour, on a 0 °C day in the default room.
SUN_ON_H = 4.0
SUN_RAMP_H = 1.0
SUN_HOLD_H = 4.0


def _sun(peak_k_per_min: float) -> Callable[[float], float]:
    """Return the free heat (K/min) of a sunny afternoon at each hour."""
    rise_end = SUN_ON_H + SUN_RAMP_H
    fall_start = rise_end + SUN_HOLD_H
    fall_end = fall_start + SUN_RAMP_H

    def free_heat(hour: float) -> float:
        if hour < SUN_ON_H or hour >= fall_end:
            return 0.0
        if hour < rise_end:
            return peak_k_per_min * (hour - SUN_ON_H) / SUN_RAMP_H
        if hour < fall_start:
            return peak_k_per_min
        return peak_k_per_min * (fall_end - hour) / SUN_RAMP_H

    return free_heat


@pytest.mark.parametrize(
    ("peak_k_per_min", "max_overshoot_k", "max_dip_k"),
    [
        pytest.param(0.02, 0.3, 0.25, id="sun0.02"),
        pytest.param(0.04, 0.45, 0.4, id="sun0.04"),
    ],
)
def test_passing_sun_neither_overheats_the_room_nor_leaves_it_cold(
    peak_k_per_min: float, max_overshoot_k: float, max_dip_k: float
) -> None:
    """The room rides out a sunny afternoon close to its setpoint.

    While the sun rises and holds, the controller has to learn the gain
    quickly enough to close the valve before the room overheats; once it
    sets, it has to forget it quickly enough to reopen the valve before the
    room cools. Both excursions are bounded for the rest of the run.
    """
    trace = _simulate(
        plant=PlantParams(),
        outdoor=0.0,
        free_heat_k_per_min=_sun(peak_k_per_min),
        setpoint_at=lambda h: SETPOINT_C,
        start=SETPOINT_C,
    )
    during = trace.indices_from(SUN_ON_H)
    sun_end_h = SUN_ON_H + 2.0 * SUN_RAMP_H + SUN_HOLD_H
    after = trace.indices_from(sun_end_h)
    overshoot = max(trace.room[i] - SETPOINT_C for i in during)
    dip = max(SETPOINT_C - trace.room[i] for i in after)
    # The run has to extend well past sunset for the dip to be seen.
    assert trace.hours[-1] - sun_end_h >= 5.0
    # The valve has to throttle well below its pre-sun opening, or the gain
    # was too small to test anything.
    before_sun = trace.valve_percent[during[0] - 1]
    assert min(trace.valve_percent[i] for i in during) <= before_sun - 20

    assert overshoot <= max_overshoot_k, f"room {overshoot:+.2f} K over the setpoint"
    assert dip <= max_dip_k, f"room {dip:.2f} K under the setpoint after sunset"
