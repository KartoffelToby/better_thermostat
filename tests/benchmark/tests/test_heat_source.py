"""The heat source: a flow temperature that moves and interruptions of the heat."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.benchmark.adapters.base import (
    BenchmarkContext,
    BenchmarkOutput,
    ControllerFamily,
)
from tests.benchmark.adapters.mpc_v2_adapter import MpcV2Adapter
from tests.benchmark.multi_trv_plant import (
    MultiTrvPlant,
    MultiTrvPlantParams,
    MultiTrvPlantState,
)
from tests.benchmark.plant import (
    PROFILE_HEAT_PUMP_CURVE,
    PROFILE_STANDARD,
    PlantState,
    TwoStatePlant,
)
from tests.benchmark.runner import run_scenario
from tests.benchmark.scenarios import (
    S01_SETPOINT_STEP_SMALL,
    S39_HEAT_PUMP_HEATING_CURVE,
    S40_HEAT_PUMP_HOT_WATER,
)


def _run_open_valve(plant: TwoStatePlant, **step_kwargs: Any) -> PlantState:
    for _ in range(60):
        plant.step(30.0, 1.0, 5.0, **step_kwargs)
    return plant.state


def test_flow_temperature_replaces_the_plant_water_temperature() -> None:
    """A step at 35 °C flow matches a plant whose own ``T_water`` is 35 °C."""
    moved = _run_open_valve(
        TwoStatePlant(PROFILE_STANDARD, PlantState(T_room=20.0, T_rad=20.0)),
        flow_temperature=35.0,
    )
    built = _run_open_valve(
        TwoStatePlant(
            replace(PROFILE_STANDARD, T_water=35.0), PlantState(T_room=20.0, T_rad=20.0)
        )
    )
    assert moved == built


def test_no_heat_delivered_cools_the_radiator_like_a_closed_valve() -> None:
    """With the heat source off an open valve heats nothing."""
    interrupted = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room=20.0, T_rad=40.0))
    closed = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room=20.0, T_rad=40.0))
    for _ in range(60):
        interrupted.step(30.0, 1.0, 5.0, heat_delivered=False)
        closed.step(30.0, 0.0, 5.0)
    assert interrupted.state == closed.state


def test_multi_trv_plant_follows_the_heat_source() -> None:
    """The multi-TRV plant takes the same two heat-source inputs."""
    params = MultiTrvPlantParams(
        n_trvs=2,
        tau_room_min=480.0,
        tau_rad_min=15.0,
        gain_heaters=[1.0, 1.0],
        coupling_rad_room=[0.5, 0.5],
        T_water=65.0,
        trv_sensor_offsets_K=[0.0, 0.0],
    )

    def _run(**step_kwargs: Any) -> MultiTrvPlantState:
        plant = MultiTrvPlant(
            params, MultiTrvPlantState(T_room=20.0, T_rads=[30.0, 30.0])
        )
        for _ in range(60):
            plant.step(30.0, [1.0, 1.0], 5.0, **step_kwargs)
        return plant.state

    warm = _run()
    low = _run(flow_temperature=35.0)
    off = _run(heat_delivered=False)
    assert off.T_rads[0] < low.T_rads[0] < warm.T_rads[0]


class _Recorder:
    """Records the heat source each context reports and keeps the valve open."""

    name: str = "recorder"
    family: ControllerFamily = "valve"

    def __init__(self) -> None:
        self.seen: list[tuple[float, float | None, float | None, bool]] = []
        self.trv: dict[float, float | None] = {}

    def reset(self, prior: dict[str, Any] | None = None) -> None:
        _ = prior

    def step(self, ctx: BenchmarkContext) -> BenchmarkOutput:
        self.seen.append(
            (
                ctx.t,
                ctx.flow_temperature,
                ctx.nominal_flow_temperature,
                ctx.heat_delivered,
            )
        )
        self.trv[ctx.t] = ctx.trv_internal_temperature
        return BenchmarkOutput(valve_percent=100.0)

    def export_state(self) -> dict[str, Any]:
        return {}


def test_the_context_reports_the_scheduled_heat_source() -> None:
    """The runner hands the flow temperature and the interruptions on."""
    scenario = replace(S40_HEAT_PUMP_HOT_WATER, duration_minutes=7 * 60)
    recorder = _Recorder()
    run_scenario(recorder, scenario, stabilisation_min=0.0)
    schedule = scenario.flow_temperature_schedule
    assert schedule is not None
    for t, flow, nominal, delivered in recorder.seen:
        assert flow == pytest.approx(schedule(t))
        assert nominal == PROFILE_HEAT_PUMP_CURVE.T_water
        assert delivered == (not 6 * 3600.0 <= t < 6.75 * 3600.0)


def test_an_interruption_cools_the_radiator_behind_an_open_valve() -> None:
    """The plant, not only the context, loses the heat while hot water is made."""
    scenario = replace(S40_HEAT_PUMP_HOT_WATER, duration_minutes=7 * 60)
    recorder = _Recorder()
    run_scenario(recorder, scenario, stabilisation_min=0.0)
    before, start, end = (recorder.trv[h * 3600.0] for h in (5.5, 6.0, 6.75))
    assert before is not None and start is not None and end is not None
    assert start >= before
    assert end < start - 1.0


def test_without_a_schedule_the_context_reports_the_plant_water_temperature() -> None:
    """A scenario without a heat source schedule keeps a steady, delivering source."""
    recorder = _Recorder()
    run_scenario(
        recorder,
        replace(S01_SETPOINT_STEP_SMALL, duration_minutes=10),
        stabilisation_min=0.0,
    )
    assert {
        (flow, nominal, delivered) for _, flow, nominal, delivered in recorder.seen
    } == {(PROFILE_STANDARD.T_water, PROFILE_STANDARD.T_water, True)}


def test_the_heating_curve_moves_the_flow_within_its_range() -> None:
    """S39's flow temperature follows the weather and stays within the curve's bounds."""
    schedule = S39_HEAT_PUMP_HEATING_CURVE.flow_temperature_schedule
    assert schedule is not None
    flows = [
        schedule(float(t))
        for t in range(0, S39_HEAT_PUMP_HEATING_CURVE.duration_minutes * 60, 600)
    ]
    assert 25.0 <= min(flows) < max(flows) <= 45.0
    assert max(flows) - min(flows) > 5.0


def _plant_water_temperature(adapter: MpcV2Adapter, nominal: float) -> float:
    ctx = BenchmarkContext(
        t=0.0,
        dt=30.0,
        target_temperature=21.0,
        room_temperature=20.0,
        raw_room_temperature=20.0,
        trv_temperature=20.0,
        outdoor_temperature=5.0,
        nominal_flow_temperature=nominal,
    )
    adapter.step(ctx)
    controller = adapter._state.controller
    assert controller is not None
    return controller.plant_coarse.params.T_water


def test_the_configured_variant_runs_on_the_nominal_flow_temperature() -> None:
    """``configure_flow_temperature`` hands the controller the nominal value."""
    configured = MpcV2Adapter(configure_flow_temperature=True)
    assert configured.name == "mpc_v2+flow_temperature"
    assert _plant_water_temperature(configured, 35.0) == 35.0


def test_the_default_variant_keeps_the_prior_flow_temperature() -> None:
    """Without the option the controller keeps the default prior."""
    default = MpcV2Adapter()
    assert default.name == "mpc_v2"
    assert _plant_water_temperature(default, 35.0) == 65.0
