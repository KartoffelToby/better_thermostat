"""Production controllers run on Better Thermostat's control cycles only."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, override

import pytest

from custom_components.better_thermostat.core.watchdog import CONTROL_TICK_S
from tests.benchmark import schedules
from tests.benchmark.adapters.base import (
    BenchmarkContext,
    BenchmarkOutput,
    ControlCycleGate,
    ControllerFamily,
)
from tests.benchmark.adapters.heating_power_adapter import HeatingPowerAdapter
from tests.benchmark.runner import run_scenario
from tests.benchmark.scenarios import (
    S01_SETPOINT_STEP_SMALL,
    InitialConditions,
    ScenarioConfig,
)


class _CountingController(ControlCycleGate):
    """Outputs how often it has been computed."""

    name: str = "counting"
    family: ControllerFamily = "valve"

    def __init__(self) -> None:
        self.computed = 0

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        _ = prior
        self._held = None
        self.computed = 0

    @override
    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        self.computed += 1
        return BenchmarkOutput(valve_percent=float(self.computed))

    def export_state(self) -> dict[str, Any]:
        return {}


class _TickFreeController(_CountingController):
    runs_on_tick = False


def _ctx(*, event: bool, tick: bool) -> BenchmarkContext:
    return BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temperature=21.0,
        room_temperature=20.0,
        raw_room_temperature=20.0,
        trv_temperature=22.0,
        outdoor_temperature=5.0,
        control_event=event,
        control_tick=tick,
    )


def test_the_first_step_computes_without_a_cycle() -> None:
    controller = _CountingController()
    assert controller.step(_ctx(event=False, tick=False)).valve_percent == 1.0


def test_the_output_holds_between_cycles() -> None:
    controller = _CountingController()
    controller.step(_ctx(event=True, tick=False))
    assert controller.step(_ctx(event=False, tick=False)).valve_percent == 1.0
    assert controller.computed == 1


@pytest.mark.parametrize(("event", "tick"), [(True, False), (False, True)])
def test_an_event_or_the_tick_computes(event: bool, tick: bool) -> None:
    controller = _CountingController()
    controller.step(_ctx(event=True, tick=False))
    assert controller.step(_ctx(event=event, tick=tick)).valve_percent == 2.0


def test_a_mode_without_the_tick_holds_through_it() -> None:
    controller = _TickFreeController()
    controller.step(_ctx(event=True, tick=False))
    assert controller.step(_ctx(event=False, tick=True)).valve_percent == 1.0


def test_heating_power_registers_no_tick() -> None:
    assert HeatingPowerAdapter.runs_on_tick is False


def test_reset_computes_on_the_next_step() -> None:
    controller = _CountingController()
    controller.step(_ctx(event=True, tick=False))
    controller.reset()
    assert controller.step(_ctx(event=False, tick=False)).valve_percent == 1.0


class _RecordingController(_CountingController):
    def __init__(self) -> None:
        super().__init__()
        self.seen: list[BenchmarkContext] = []

    @override
    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        self.seen.append(ctx)
        return super().step(ctx)


class _ClosedValveRecorder(_RecordingController):
    """Keeps the valve closed, so a room at outdoor temperature stays still."""

    @override
    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        return BenchmarkOutput(valve_percent=0.0)


_QUIET_ROOM = ScenarioConfig(
    name="quiet_room",
    description="Room at its 21 °C target, radiator at room temperature",
    duration_minutes=30,
    initial=InitialConditions(T_room=21.0, T_rad=21.0),
    plant=S01_SETPOINT_STEP_SMALL.plant,
    setpoint_schedule=schedules.constant(21.0),
    outdoor_schedule=schedules.constant(21.0),
    stabilisation_min=0.0,
)


def _flags(scenario: ScenarioConfig) -> dict[float, tuple[bool, bool]]:
    controller = _ClosedValveRecorder()
    run_scenario(controller, scenario, step_s=30.0)
    return {c.t: (c.control_event, c.control_tick) for c in controller.seen}


def test_the_runner_ticks_every_control_tick() -> None:
    flags = _flags(_QUIET_ROOM)
    ticks = [t for t, (_, tick) in flags.items() if tick]
    assert ticks == [k * CONTROL_TICK_S for k in range(len(ticks))]
    assert len(ticks) == 30 * 60 // int(CONTROL_TICK_S) + 1


def test_a_still_room_raises_no_event_after_the_first_step() -> None:
    flags = _flags(_QUIET_ROOM)
    assert flags[0.0][0] is True
    assert not any(event for t, (event, _) in flags.items() if t > 0.0)


def test_a_setpoint_change_raises_an_event() -> None:
    scenario = replace(
        _QUIET_ROOM,
        setpoint_schedule=schedules.piecewise_step([(600.0, 22.0)], initial=21.0),
    )
    flags = _flags(scenario)
    assert flags[600.0][0] is True
    assert flags[630.0][0] is False


def test_the_runner_writes_a_direct_valve_as_a_whole_percent() -> None:
    controller = _FractionalValve()
    result = run_scenario(controller, _QUIET_ROOM, step_s=30.0)
    # 37.4 % rounds to 37 % over the whole 30 min.
    assert result.metrics.integral_valve_percent_minutes == pytest.approx(37.0 * 30)


class _FractionalValve(_CountingController):
    @override
    def _compute(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        return BenchmarkOutput(valve_percent=37.4)
