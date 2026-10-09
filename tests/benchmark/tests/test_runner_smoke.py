"""End-to-end runs through the shared drive loop."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, override

import pytest

from tests.benchmark import schedules
from tests.benchmark.actuator import Actuator, ActuatorParams
from tests.benchmark.adapters.base import (
    BenchmarkContext,
    BenchmarkOutput,
    ControllerFamily,
)
from tests.benchmark.adapters.mpc_adapter import MpcAdapter
from tests.benchmark.multi_trv_plant import PROFILE_MULTI_SYMMETRIC, MultiTrvPlantState
from tests.benchmark.multi_trv_runner import (
    make_multi_trv_adapter,
    run_multi_trv_scenario,
)
from tests.benchmark.plant import PlantState, TwoStatePlant
from tests.benchmark.runner import (
    ADAPTER_FACTORIES,
    _drive_adapter,
    _make_adapter,
    _SingleTrvFacade,
    run_scenario,
)
from tests.benchmark.scenarios import (
    S01_SETPOINT_STEP_SMALL,
    InitialConditions,
    ScenarioConfig,
)


def test_mpc_against_s01_runs_to_completion():
    """End-to-end: MPC adapter runs through S01 without crashes."""
    adapter = MpcAdapter()
    result = run_scenario(adapter, S01_SETPOINT_STEP_SMALL)
    # Smoke-level expectations only — assert nothing crashed and produced
    # something sensible.
    assert result.controller == "mpc"
    assert result.scenario == S01_SETPOINT_STEP_SMALL.name
    # Metric values are finite numbers (settling may legitimately be inf if
    # the algorithm fails to converge — we don't assert PASS here).
    m = result.metrics
    # Finite (not NaN/inf) is the real invariant; >= 0 alone would admit inf.
    assert math.isfinite(m.max_overshoot_K)
    assert math.isfinite(m.max_undershoot_K)
    assert (m.settling_time_min >= 0.0) or math.isinf(m.settling_time_min)
    assert math.isfinite(m.rmse_tracking_K)
    assert math.isfinite(m.valve_cycle_count)
    assert math.isfinite(m.integral_valve_pct_min)


def test_mpc_run_is_deterministic():
    """Two independent MPC runs of S01 produce byte-identical metrics.

    The production MPC uses ``random.random()`` for its hybrid-learning
    forced calibration; the adapter seeds a deterministic stand-in so the
    benchmark's reproducibility guarantee holds for MPC too. This guards
    against that seeding regressing.
    """
    a = run_scenario(MpcAdapter(), S01_SETPOINT_STEP_SMALL).metrics
    b = run_scenario(MpcAdapter(), S01_SETPOINT_STEP_SMALL).metrics
    assert a == b


# A room 4 K below its target with the window open for the whole run.
_COLD_ROOM_WINDOW_OPEN = ScenarioConfig(
    name="window_open_cold_room",
    description="Window open throughout, room 4 K below a 21 °C target",
    duration_min=20,
    initial=InitialConditions(T_room=17.0, T_rad=17.0),
    plant=S01_SETPOINT_STEP_SMALL.plant,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(5.0),
    window_open_schedule=lambda _t: True,
    stabilisation_min=0.0,
)


@pytest.mark.parametrize("controller", sorted(ADAPTER_FACTORIES))
def test_open_window_closes_the_valve_for_every_controller(controller):
    """No registered controller heats a cold room through an open window.

    Better Thermostat turns the TRVs off while a window is open, whatever
    the calibration mode, so the plant sees a closed valve for the whole run.
    """
    result = run_scenario(
        _make_adapter(controller, _COLD_ROOM_WINDOW_OPEN.plant), _COLD_ROOM_WINDOW_OPEN
    )
    assert result.metrics.integral_valve_pct_min == 0.0


def test_open_window_closes_the_valve_on_a_multi_trv_plant():
    """The multi-TRV block drives the same loop and closes every radiator."""
    plant = PROFILE_MULTI_SYMMETRIC
    result = run_multi_trv_scenario(
        make_multi_trv_adapter("pid", plant),
        _COLD_ROOM_WINDOW_OPEN,
        plant_params=plant,
        initial_state=MultiTrvPlantState(T_room=17.0, T_rads=[17.0] * plant.n_trvs),
    )
    assert result.metrics.integral_valve_pct_min == 0.0


class _ConstantValveAdapter:
    """Commands a fixed valve and records every context it is handed."""

    name: str = "constant_valve"
    family: ControllerFamily = "valve"

    def __init__(self, valve_percent: float) -> None:
        self._valve_percent = valve_percent
        self.seen: list[BenchmarkContext] = []

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        _ = prior
        self.seen.clear()

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        self.seen.append(ctx)
        return BenchmarkOutput(valve_percent=self._valve_percent)

    def export_state(self) -> dict[str, Any]:
        return {}


def test_open_window_overrides_the_valve_only_while_open():
    """The controller keeps running through the window; only the plant input closes.

    The controller is told the window is open, and each step hands it the
    valve the plant received on the step before: closed while the window
    is open, its own command again once the window closes.
    """
    scenario = replace(
        _COLD_ROOM_WINDOW_OPEN,
        window_open_schedule=schedules.pulse_bool(5 * 60.0, 10 * 60.0),
    )
    adapter = _ConstantValveAdapter(60.0)
    plant = TwoStatePlant(scenario.plant, PlantState(T_room=17.0, T_rad=17.0))
    facade = _SingleTrvFacade(plant, Actuator(ActuatorParams()))

    _drive_adapter(
        adapter, facade, scenario, 60.0, 15 * 60.0, handle_controller_restart=False
    )

    seen = {round(ctx.t / 60.0): ctx for ctx in adapter.seen}
    assert [seen[m].window_open for m in (4, 5, 9, 10)] == [False, True, True, False]
    # The valve applied at minutes 4, 5, 9 and 10.
    applied = [seen[m + 1].last_valve_percent for m in (4, 5, 9, 10)]
    assert applied == [60.0, 0.0, 0.0, 60.0]


class _RecordingActuator(Actuator):
    """Records the flow each ``apply`` hands to the plant."""

    def __init__(self, params: ActuatorParams) -> None:
        super().__init__(params)
        self.flows: list[float] = []

    @override
    def apply(self, cmd_pct: float) -> float:
        flow = super().apply(cmd_pct)
        self.flows.append(flow)
        return flow


class _ScheduledValveAdapter(_ConstantValveAdapter):
    """Commands 60 % for two minutes, 5 % for two, 8 % until minute 10, then 3 %."""

    @override
    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        self.seen.append(ctx)
        minute = ctx.t / 60.0
        if minute < 2:
            return BenchmarkOutput(valve_percent=60.0)
        if minute < 4:
            return BenchmarkOutput(valve_percent=5.0)
        if minute < 10:
            return BenchmarkOutput(valve_percent=8.0)
        return BenchmarkOutput(valve_percent=3.0)


def test_open_window_closes_the_plant_valve_through_actuator_hysteresis():
    """The window close reaches the plant when hysteresis would hold the valve.

    With a 10 % band, the valve moves from 60 % to 5 % and then holds 5 %
    against the 8 % command. Closing for the window is inside that band
    too, yet the plant must get 0 %, not the held 5 %. After the window,
    3 % lies inside the band around the closed valve, so it stays closed.
    """
    scenario = replace(
        _COLD_ROOM_WINDOW_OPEN,
        window_open_schedule=schedules.pulse_bool(5 * 60.0, 10 * 60.0),
    )
    adapter = _ScheduledValveAdapter(0.0)
    plant = TwoStatePlant(scenario.plant, PlantState(T_room=17.0, T_rad=17.0))
    actuator = _RecordingActuator(ActuatorParams(hysteresis_pct=10.0))
    facade = _SingleTrvFacade(plant, actuator)

    _drive_adapter(
        adapter, facade, scenario, 60.0, 15 * 60.0, handle_controller_restart=False
    )

    # One entry per minute, minutes 0 to 15.
    window = [ctx.window_open for ctx in adapter.seen]
    assert window == [False] * 5 + [True] * 5 + [False] * 6
    assert actuator.flows == [0.6] * 2 + [0.05] * 3 + [0.0] * 11
