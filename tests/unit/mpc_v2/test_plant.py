"""Unit tests for the RC2 plant model."""

from __future__ import annotations

import numpy as np

from custom_components.better_thermostat.utils import state_manager as _state_manager
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    params as _params,
    reid as _reid,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    GAIN_HEATER_BOUNDS,
    TAU_ROOM_BOUNDS_MIN,
    PlantModelRC2,
    PlantParams,
)


class TestPlantPriorBands:
    """Every producer and consumer of a plant prior reads one band."""

    def test_reid_reads_the_bands_from_plant(self) -> None:
        """The offline fit clamps its emission against these very tuples."""
        assert _reid.TAU_ROOM_BOUNDS_MIN is TAU_ROOM_BOUNDS_MIN
        assert _reid.GAIN_HEATER_BOUNDS is GAIN_HEATER_BOUNDS

    def test_state_manager_reads_the_bands_from_plant(self) -> None:
        """The restore gate rejects against these very tuples."""
        assert _state_manager.TAU_ROOM_BOUNDS_MIN is TAU_ROOM_BOUNDS_MIN
        assert _state_manager.GAIN_HEATER_BOUNDS is GAIN_HEATER_BOUNDS

    def test_heat_loss_derivation_clamps_to_the_band(self) -> None:
        """The AUTO heuristic lands on the band edges, not on its own numbers."""
        low, high = TAU_ROOM_BOUNDS_MIN
        assert _params.make_plant_prior(heat_loss_rate=1.0).tau_room_min == low
        assert _params.make_plant_prior(heat_loss_rate=0.0001).tau_room_min == high


def test_state_dim_is_two() -> None:
    """The RC2 plant reports a two-dimensional state."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    assert plant.state_dim == 2


def test_discrete_step_with_zero_u_cools_toward_outdoor() -> None:
    """With no heat the plant cools toward the outdoor temperature."""
    plant = PlantModelRC2(PlantParams(tau_room_min=120.0, tau_rad_min=10.0), dt_s=30.0)
    x = np.array([21.0, 21.0])
    T_outdoor = 5.0
    for _ in range(2000):
        x = plant.discrete_step(x, u=0.0, T_outdoor_C=T_outdoor)
    assert abs(float(x[0]) - T_outdoor) < 0.5
    assert abs(float(x[1]) - T_outdoor) < 0.5


def test_discrete_step_with_full_u_heats_toward_water() -> None:
    """At full heat the radiator approaches water temp and the room warms well above setpoint."""
    plant = PlantModelRC2(
        PlantParams(
            tau_room_min=120.0, tau_rad_min=5.0, gain_heater=5.0, T_water_C=65.0
        ),
        dt_s=30.0,
    )
    x = np.array([20.0, 20.0])
    for _ in range(2000):
        x = plant.discrete_step(x, u=1.0, T_outdoor_C=10.0)
    # Room equilibrates well above setpoint; T_rad approaches water temp.
    assert float(x[1]) > 50.0
    assert float(x[0]) > 30.0


def test_linearisation_matches_discrete_step_for_small_dt() -> None:
    """The linearised step agrees with the nonlinear step at the operating point."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    x = np.array([20.5, 35.0])
    T_outdoor, u = 5.0, 0.4
    x_next_nonlin = plant.discrete_step(x, u=u, T_outdoor_C=T_outdoor)
    A, B, d = plant.linearised_AB(T_outdoor, T_rad_op_C=float(x[1]))
    x_next_lin = A @ x + B.flatten() * u + d
    # Linearised around operating x[1], they should agree to ~1e-12.
    np.testing.assert_allclose(x_next_lin, x_next_nonlin, atol=1e-10)


def test_linearisation_stable_eigenvalues() -> None:
    """The linearised plant has all eigenvalues inside the unit circle."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    A, _, _ = plant.linearised_AB(T_outdoor_C=5.0, T_rad_op_C=30.0)
    eigs = np.linalg.eigvals(A)
    # All eigenvalues inside the unit circle ⇒ stable open-loop plant.
    assert max(abs(eigs)) < 1.0
