"""Sanity tests for the plant model."""

from __future__ import annotations

from tests.benchmark.plant import PROFILE_STANDARD, PlantState, TwoStatePlant


def test_plant_warms_with_full_valve():
    """A fully open valve heats both radiator and room over 30 min."""
    plant = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room_C=18.0, T_rad_C=18.0))
    # Run 30 minutes at fully open valve, mild outdoor.
    for _ in range(60):
        plant.step(dt_s=30.0, u=1.0, T_outdoor_C=5.0)
    assert plant.state.T_room_C > 18.5, (
        f"Room should warm with full valve, got {plant.state.T_room_C:.3f}"
    )
    assert plant.state.T_rad_C > plant.state.T_room_C, (
        "Radiator should be hotter than room when heating"
    )


def test_plant_cools_without_heat():
    """Without heat input the room cools toward the outdoor temperature."""
    plant = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room_C=22.0, T_rad_C=22.0))
    for _ in range(120):  # 1 h
        plant.step(dt_s=30.0, u=0.0, T_outdoor_C=0.0)
    assert plant.state.T_room_C < 22.0, (
        f"Room should cool without heat input, got {plant.state.T_room_C:.3f}"
    )


def test_plant_settles_at_outdoor_without_heat():
    """After many time constants, the room equilibrates to outdoor."""
    plant = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room_C=22.0, T_rad_C=22.0))
    # Run many time constants without heat — should approach outdoor.
    # PROFILE_STANDARD has tau_room=480min, so ~5 e-folds in 40h to be safe.
    for _ in range(5000):  # ~42 h with 30s steps
        plant.step(dt_s=30.0, u=0.0, T_outdoor_C=5.0)
    assert abs(plant.state.T_room_C - 5.0) < 1.0, (
        f"After long cooling room should approach outdoor=5C, got {plant.state.T_room_C:.3f}"
    )


def test_plant_zero_dt_is_noop():
    """A step with ``dt_s == 0`` must leave state unchanged."""
    plant = TwoStatePlant(PROFILE_STANDARD, PlantState(T_room_C=20.0, T_rad_C=20.0))
    before = (plant.state.T_room_C, plant.state.T_rad_C)
    plant.step(dt_s=0.0, u=1.0, T_outdoor_C=0.0)
    assert (plant.state.T_room_C, plant.state.T_rad_C) == before
