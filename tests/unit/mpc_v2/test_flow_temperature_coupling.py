"""The plant prior sizes its radiator for the configured flow temperature."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    MpcV2Controller,
    MpcV2Params,
    PlantParams,
    coupling_for_flow_temperature,
    make_plant_prior,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
)
from custom_components.better_thermostat.utils.calibration.plant_preset import (
    DEFAULT_FLOW_TEMPERATURE,
    FLOW_TEMPERATURE_BOUNDS,
)

SETPOINT = 21.0
CONTROL_STEP_S = 30.0
ROOM_STEP_S = 5.0


def _sized_prior(flow_temperature: float) -> PlantParams:
    return replace(
        make_plant_prior(),
        coupling_rad_room=coupling_for_flow_temperature(flow_temperature),
        T_water=flow_temperature,
    )


def test_the_default_flow_temperature_keeps_the_default_coupling() -> None:
    """A thermostat without a configured flow temperature models what it did."""
    assert coupling_for_flow_temperature(DEFAULT_FLOW_TEMPERATURE) == (
        PlantParams().coupling_rad_room
    )


def test_a_cooler_flow_means_a_larger_radiator() -> None:
    """The coupling grows as the flow temperature falls."""
    lower, upper = FLOW_TEMPERATURE_BOUNDS
    flows = [lower, 35.0, 45.0, DEFAULT_FLOW_TEMPERATURE, upper]
    couplings = [coupling_for_flow_temperature(flow) for flow in flows]
    assert couplings == sorted(couplings, reverse=True)
    assert coupling_for_flow_temperature(35.0) == pytest.approx(44.0 / 14.0)


@pytest.mark.parametrize("flow_temperature", [25.0, 35.0, 45.0, 55.0, 75.0, 90.0])
@pytest.mark.parametrize("outdoor_temperature", [-15.0, -5.0, 0.0, 5.0, 12.0])
def test_the_sized_radiator_holds_the_room_at_the_default_valve_opening(
    flow_temperature: float, outdoor_temperature: float
) -> None:
    """At 21 °C the steady valve opening does not depend on the flow temperature."""
    default = PlantModelRC2(make_plant_prior(), dt_s=CONTROL_STEP_S)
    sized = PlantModelRC2(_sized_prior(flow_temperature), dt_s=CONTROL_STEP_S)
    assert sized.steady_input(SETPOINT, outdoor_temperature) == pytest.approx(
        default.steady_input(SETPOINT, outdoor_temperature)
    )


def _run_day(
    prior: PlantParams, room: PlantParams, outdoor_temperature: float
) -> tuple[float, float]:
    """Return the largest deviation after 4 h and the valve travel in percent.

    The room starts at the setpoint with its radiator in steady state.
    """
    controller = MpcV2Controller(MpcV2Params(plant=prior))
    room_temperature = SETPOINT
    radiator = SETPOINT + (SETPOINT - outdoor_temperature) / room.coupling_rad_room
    travel = 0.0
    previous: float | None = None
    deviation = 0.0
    h_min = ROOM_STEP_S / 60.0
    for cycle in range(int(24 * 3600 / CONTROL_STEP_S)):
        u, _ = controller.step(
            t_s=cycle * CONTROL_STEP_S,
            T_room=room_temperature,
            T_target=SETPOINT,
            T_outdoor=outdoor_temperature,
            T_rad=radiator,
        )
        u = max(0.0, min(1.0, u))
        controller.set_applied_u(u)
        if previous is not None:
            travel += abs(u - previous)
        previous = u
        if cycle * CONTROL_STEP_S >= 4 * 3600:
            deviation = max(deviation, abs(room_temperature - SETPOINT))
        for _ in range(int(CONTROL_STEP_S / ROOM_STEP_S)):
            radiator_rate = (
                room.gain_heater * u * (room.T_water - radiator)
                - (radiator - room_temperature)
            ) / room.tau_rad_min
            room_rate = (
                room.coupling_rad_room * (radiator - room_temperature)
                - (room_temperature - outdoor_temperature)
            ) / room.tau_room_min
            radiator += radiator_rate * h_min
            room_temperature += room_rate * h_min
    return deviation, 100.0 * travel


@pytest.mark.parametrize("outdoor_temperature", [0.0, 10.0])
def test_a_heat_pump_room_on_its_configured_flow_is_held_calmly(
    outdoor_temperature: float,
) -> None:
    """35 °C flow into radiators sized for it: on target, the valve nearly still.

    A prior that kept a radiator sized for 65 °C would model 21 °C as out of
    reach below about 7 °C outdoor and move the valve by several full
    strokes a day.
    """
    room = _sized_prior(35.0)
    deviation, travel = _run_day(_sized_prior(35.0), room, outdoor_temperature)
    assert deviation < 0.05
    assert travel < 200.0
