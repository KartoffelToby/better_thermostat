"""Tests for the RC3 plant extension and the EQUAL_PERCENTAGE actuator profile (Phase D.1)."""

from __future__ import annotations

from dataclasses import replace

from tests.benchmark.actuator import Actuator, ActuatorParams, ActuatorProfile
from tests.benchmark.plant import (
    PROFILE_STANDARD,
    PROFILE_STANDARD_RC3,
    PlantParams,
    PlantState,
    TwoStatePlant,
)

# ---------- RC3 plant ----------


def test_rc3_plant_initialises_wall_to_room_when_unset():
    """When PlantState.T_wall_C is None, the plant copies T_room_C onto the wall."""
    plant = TwoStatePlant(
        PROFILE_STANDARD_RC3, PlantState(T_room_C=20.0, T_rad_C=20.0, T_wall_C=None)
    )
    assert plant.state.T_wall_C == 20.0


def test_rc3_plant_warms_with_full_valve():
    """A full-valve RC3 plant heats both radiator and room over 60 min."""
    plant = TwoStatePlant(PROFILE_STANDARD_RC3, PlantState(T_room_C=18.0, T_rad_C=18.0))
    for _ in range(120):  # 60 min at 30s steps
        plant.step(30.0, 1.0, 5.0)
    assert plant.state.T_room_C > 18.5
    assert plant.state.T_rad_C > plant.state.T_room_C


def test_rc3_wall_lags_room_during_heating():
    """During heating, the wall warms much more slowly than the room.

    Sanity for the RC3 splitting: the wall is between room and outdoor,
    and starts at room temp here. Because the wall→outdoor gradient is
    initially much bigger than the room→wall gradient, the wall actually
    drifts toward outdoor while the room warms. The key sanity check is
    that the room is always warmer than the wall (otherwise the room→wall
    heat flow has the wrong sign for our physical model).
    """
    plant = TwoStatePlant(PROFILE_STANDARD_RC3, PlantState(T_room_C=18.0, T_rad_C=18.0))
    for _ in range(40):  # 20 min at 30s
        plant.step(30.0, 1.0, 5.0)
    assert plant.state.T_room_C is not None
    assert plant.state.T_wall_C is not None
    # Room is heating fast, wall lags far behind.
    assert plant.state.T_room_C > plant.state.T_wall_C
    # Room has warmed measurably.
    assert plant.state.T_room_C > 19.0


def test_rc3_room_cools_slower_than_rc2_with_same_lumped_tau():
    """RC3 with a heat-loaded wall holds the room longer than the equivalent RC2 plant.

    Construct an RC3 plant whose total room+wall thermal mass is roughly
    comparable to an RC2 plant with tau_room=600. After full warming, cut
    the heater off and compare the room temperatures after 2 hours. RC3
    should still be warmer because the wall is acting as a battery.
    """
    rc2 = PlantParams(
        tau_room_min=600.0,
        tau_rad_min=15.0,
        gain_heater=2.0,
        coupling_rad_room=1.0,
        T_water_C=65.0,
    )
    rc3 = PlantParams(
        tau_room_min=60.0,
        tau_rad_min=15.0,
        gain_heater=2.0,
        coupling_rad_room=1.0,
        T_water_C=65.0,
        tau_wall_min=900.0,
        r_room_wall=1.0,
    )

    plant_rc2 = TwoStatePlant(rc2, PlantState(T_room_C=22.0, T_rad_C=35.0))
    plant_rc3 = TwoStatePlant(
        rc3, PlantState(T_room_C=22.0, T_rad_C=35.0, T_wall_C=22.0)
    )

    # Pre-warm wall in RC3 so it really acts like a battery — heat for 4 h
    # against a steady setpoint by injecting some heater command.
    for _ in range(4 * 60 * 2):  # 4 h at 30 s
        plant_rc3.step(30.0, 0.25, 5.0)
    for _ in range(4 * 60 * 2):
        plant_rc2.step(30.0, 0.25, 5.0)

    # Now switch heater off and let both cool for 2 h.
    for _ in range(2 * 60 * 2):  # 2 h
        plant_rc2.step(30.0, 0.0, 5.0)
        plant_rc3.step(30.0, 0.0, 5.0)

    # RC3 should retain more heat thanks to the wall capacitance.
    assert plant_rc3.state.T_room_C > plant_rc2.state.T_room_C


def test_rc2_path_unchanged_when_tau_wall_zero():
    """``tau_wall_min == 0`` follows the RC2 reference exactly.

    Non-default wall parameters must have no effect while the wall layer
    is disabled, so both plants must produce bit-identical trajectories.
    """
    reference = TwoStatePlant(
        PROFILE_STANDARD,  # RC2 default (tau_wall_min == 0)
        PlantState(T_room_C=20.0, T_rad_C=20.0),
    )
    tau_zero = TwoStatePlant(
        replace(PROFILE_STANDARD, tau_wall_min=0.0, r_room_wall=3.7),
        PlantState(T_room_C=20.0, T_rad_C=20.0),
    )
    for _ in range(60):
        reference.step(30.0, 0.5, 5.0)
        tau_zero.step(30.0, 0.5, 5.0)
        assert tau_zero.state.T_room_C == reference.state.T_room_C
        assert tau_zero.state.T_rad_C == reference.state.T_rad_C
    # Sanity check that RC2 still warms.
    assert 20.0 < reference.state.T_room_C < 35.0


# ---------- EQUAL_PERCENTAGE actuator ----------


def test_equal_percentage_low_pct_gives_small_flow():
    """At 10% command, equal-percentage flow is much less than linear."""
    actuator = Actuator(
        ActuatorParams(
            profile=ActuatorProfile.EQUAL_PERCENTAGE, equal_percentage_exponent=3.0
        )
    )
    flow = actuator.apply(10.0)
    # 0.1^3 = 0.001 — three orders of magnitude smaller than the 0.1 linear case.
    assert flow < 0.05


def test_equal_percentage_high_pct_approaches_full_flow():
    """At 90% command, equal-percentage gives roughly 0.7 flow."""
    actuator = Actuator(
        ActuatorParams(
            profile=ActuatorProfile.EQUAL_PERCENTAGE, equal_percentage_exponent=3.0
        )
    )
    flow = actuator.apply(90.0)
    # 0.9^3 = 0.729
    assert 0.6 < flow < 0.8


def test_equal_percentage_full_command_full_flow():
    """At 100% command, equal-percentage delivers full flow."""
    actuator = Actuator(ActuatorParams(profile=ActuatorProfile.EQUAL_PERCENTAGE))
    flow = actuator.apply(100.0)
    assert flow == 1.0


def test_equal_percentage_zero_command_zero_flow():
    """At 0% command, equal-percentage delivers no flow."""
    actuator = Actuator(ActuatorParams(profile=ActuatorProfile.EQUAL_PERCENTAGE))
    flow = actuator.apply(0.0)
    assert flow == 0.0


def test_equal_percentage_exponent_changes_curve():
    """A higher exponent makes the curve more aggressive at the low end."""
    flow_low = Actuator(
        ActuatorParams(
            profile=ActuatorProfile.EQUAL_PERCENTAGE, equal_percentage_exponent=2.0
        )
    ).apply(50.0)
    flow_high = Actuator(
        ActuatorParams(
            profile=ActuatorProfile.EQUAL_PERCENTAGE, equal_percentage_exponent=4.0
        )
    ).apply(50.0)
    # At 50%: 0.5^2 = 0.25 vs 0.5^4 = 0.0625 → higher exponent gives lower flow.
    assert flow_low > flow_high
