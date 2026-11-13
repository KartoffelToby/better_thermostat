"""MPC v2 against a room its supply water cannot hold at the setpoint.

The simulated room has exactly the RC2 structure and parameters the
controller assumes, so every shortfall below the setpoint is physical: a fully
open valve is the best any controller can do. The room is integrated in
5-second Euler steps and the controller runs every 30 seconds for 24 hours.
"""

from __future__ import annotations

import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2.controller import (
    MpcV2Controller,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2.params import (
    MpcV2Params,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantParams,
)

SETPOINT = 21.0
CONTROL_STEP_S = 30.0
ROOM_STEP_S = 5.0
HOURS = 24.0


def _run(
    plant: PlantParams,
    outdoor_temperature: float,
    start_temperature: float,
    setpoint: float = SETPOINT,
) -> tuple[float, float]:
    """Return the final room temperature and the mean valve fraction."""
    controller = MpcV2Controller(MpcV2Params(plant=plant))
    room = radiator = start_temperature
    valve_sum = 0.0
    cycles = int(HOURS * 3600.0 / CONTROL_STEP_S)
    substeps = int(CONTROL_STEP_S / ROOM_STEP_S)
    h_min = ROOM_STEP_S / 60.0
    for cycle in range(cycles):
        u, _ = controller.step(
            t_s=cycle * CONTROL_STEP_S,
            T_room=room,
            T_target=setpoint,
            T_outdoor=outdoor_temperature,
            T_rad=radiator,
        )
        u = max(0.0, min(1.0, u))
        controller.set_applied_u(u)
        valve_sum += u
        for _ in range(substeps):
            radiator_rate = (
                plant.gain_heater * u * (plant.T_water - radiator) - (radiator - room)
            ) / plant.tau_rad_min
            room_rate = (
                plant.coupling_rad_room * (radiator - room)
                - (room - outdoor_temperature)
            ) / plant.tau_room_min
            radiator += radiator_rate * h_min
            room += room_rate * h_min
    return room, valve_sum / cycles


def _warmest_holdable_room(plant: PlantParams, outdoor_temperature: float) -> float:
    """Return the room temperature a fully open valve settles the room at."""
    g = plant.gain_heater
    c = plant.coupling_rad_room
    return (g * plant.T_water + (1.0 + g) * outdoor_temperature / c) / (
        g + (1.0 + g) / c
    )


@pytest.mark.parametrize("start_temperature", [21.0, 17.0])
def test_low_supply_water_on_a_cold_day_opens_the_valve_fully(
    start_temperature: float,
) -> None:
    """33 °C supply water at -20 °C outdoors reaches the physical maximum.

    The setpoint needs a radiator of 34.7 °C, above the water. A room that
    starts on the setpoint gives the reference governor no room to shape the
    reference, so the optimiser plans with the unreachable setpoint itself and
    must still see that opening the valve heats.
    """
    plant = PlantParams(
        tau_room_min=480.0,
        tau_rad_min=15.0,
        gain_heater=2.0,
        coupling_rad_room=3.0,
        T_water=33.0,
    )
    outdoor = -20.0
    warmest = _warmest_holdable_room(plant, outdoor)
    assert warmest == pytest.approx(15.333, abs=1e-3)

    final_room, mean_valve = _run(plant, outdoor, start_temperature)

    assert mean_valve > 0.9
    assert final_room == pytest.approx(warmest, abs=0.01)


def test_setpoint_above_the_supply_water_does_not_shut_the_valve() -> None:
    """A room below a setpoint above the supply water gets an open valve.

    No radiator gets warmer than 24 °C water, so the 26 °C setpoint is out of
    reach. The room cools from the setpoint until the water can warm it; the
    valve must stay open over that stretch, not close because the model reads
    an opening as cooling.
    """
    plant = PlantParams(
        tau_room_min=480.0,
        tau_rad_min=15.0,
        gain_heater=2.0,
        coupling_rad_room=3.0,
        T_water=24.0,
    )

    final_room, mean_valve = _run(plant, 5.0, 26.0, setpoint=26.0)

    assert mean_valve > 0.3
    assert final_room > 16.0


# Final room temperature and mean valve fraction with 65 °C supply water, for
# a radiator coupling of 1.0 at which a setpoint of 21 °C runs out of reach
# below about -10 °C outdoors. Unreachable setpoints there are shaped by the
# reference governor; the values pin that the supply-water handling leaves
# that path untouched. They are recorded on the default two-hour, two-step
# move-blocked horizon.
_SUPPLY_65_ROWS = [
    (2.0, 0.0, 17.0, 21.000001062987295, 0.5208547256711605),
    (2.0, 0.0, 21.0, 21.000000116813144, 0.46121509411648465),
    (2.0, -5.0, 17.0, 21.000001254549794, 0.7843982973465022),
    (2.0, -5.0, 21.0, 21.000000104210322, 0.7306858543612301),
    (2.0, -10.0, 17.0, 19.97584786066472, 0.9977430555555558),
    (2.0, -10.0, 21.0, 20.003556375562656, 0.9977430555555555),
    (2.0, -15.0, 17.0, 16.99640452996068, 0.9977430555555555),
    (2.0, -15.0, 21.0, 17.024113044858684, 0.9977430555555554),
    (1.0, 0.0, 17.0, 20.999947310226357, 0.9500952890883032),
    (1.0, 0.0, 21.0, 21.000001676814808, 0.9209958541997701),
    (1.0, -5.0, 17.0, 18.311859876410622, 0.9977430555555554),
    (1.0, -5.0, 21.0, 18.358387011001284, 0.9977430555555554),
    (1.0, -10.0, 17.0, 15.016873125803366, 0.9977430555555555),
    (1.0, -10.0, 21.0, 15.063400260394046, 0.9977430555555555),
    (1.0, -15.0, 17.0, 11.721886375196071, 0.9977430555555555),
    (1.0, -15.0, 21.0, 11.768413509786793, 0.9977430555555554),
    (0.5, 0.0, 17.0, 16.25592894504447, 0.9977430555555555),
    (0.5, 0.0, 21.0, 16.33408586144374, 0.9977430555555555),
    (0.5, -5.0, 17.0, 12.578529863647239, 0.9977430555555555),
    (0.5, -5.0, 21.0, 12.656686780046556, 0.9977430555555555),
    (0.5, -10.0, 17.0, 8.901130782250029, 0.9977430555555558),
    (0.5, -10.0, 21.0, 8.979287698649324, 0.9977430555555555),
    (0.5, -15.0, 17.0, 5.2237317008527615, 0.9977430555555555),
    (0.5, -15.0, 21.0, 5.301888617252095, 0.9977430555555555),
]


@pytest.mark.parametrize(
    ("gain", "outdoor", "start", "final_room", "mean_valve"), _SUPPLY_65_ROWS
)
def test_default_supply_water_trajectories_are_pinned(
    gain: float, outdoor: float, start: float, final_room: float, mean_valve: float
) -> None:
    """65 °C supply water reproduces the pinned final room and mean valve."""
    plant = PlantParams(
        tau_room_min=480.0,
        tau_rad_min=15.0,
        gain_heater=gain,
        coupling_rad_room=1.0,
        T_water=65.0,
    )

    room, valve = _run(plant, outdoor, start)

    assert room == pytest.approx(final_room, rel=1e-9, abs=1e-9)
    assert valve == pytest.approx(mean_valve, rel=1e-9, abs=1e-9)
