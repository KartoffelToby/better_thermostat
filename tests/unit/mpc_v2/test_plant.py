"""Unit tests for the RC2 plant model."""

from __future__ import annotations

from collections.abc import Callable
import math
from pathlib import Path
import sys
from types import FrameType
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from custom_components.better_thermostat.utils import state_manager as _state_manager
from custom_components.better_thermostat.utils.calibration.mpc_v2 import (
    params as _params,
    reid as _reid,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
    plant as _plant,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    GAIN_HEATER_BOUNDS,
    TAU_ROOM_BOUNDS_MIN,
    PlantModelRC2,
    PlantParams,
)

if TYPE_CHECKING:
    from sys import TraceFunction


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
        x = plant.discrete_step(x, u=0.0, T_outdoor=T_outdoor)
    assert abs(float(x[0]) - T_outdoor) < 0.5
    assert abs(float(x[1]) - T_outdoor) < 0.5


def test_discrete_step_with_full_u_heats_toward_water() -> None:
    """At full heat the radiator approaches water temperature and the room warms well above setpoint."""
    plant = PlantModelRC2(
        PlantParams(tau_room_min=120.0, tau_rad_min=5.0, gain_heater=5.0, T_water=65.0),
        dt_s=30.0,
    )
    x = np.array([20.0, 20.0])
    for _ in range(2000):
        x = plant.discrete_step(x, u=1.0, T_outdoor=10.0)
    # Room equilibrates well above setpoint; T_rad approaches water temperature.
    assert float(x[1]) > 50.0
    assert float(x[0]) > 30.0


def test_linearisation_matches_discrete_step_for_small_dt() -> None:
    """The linearised step agrees with the nonlinear step at the operating point."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    x = np.array([20.5, 35.0])
    T_outdoor, u = 5.0, 0.4
    x_next_nonlin = plant.discrete_step(x, u=u, T_outdoor=T_outdoor)
    A, B, d = plant.linearised_system(T_outdoor, T_rad_op=float(x[1]))
    x_next_lin = A @ x + B.flatten() * u + d
    # Linearised around operating x[1], they should agree to ~1e-12.
    np.testing.assert_allclose(x_next_lin, x_next_nonlin, atol=1e-10)


def test_long_linearised_interval_is_composed_from_stable_substeps() -> None:
    """A sparse observer update stays finite even across a one-hour gap."""
    plant = PlantModelRC2(PlantParams(tau_rad_min=15.0), dt_s=30.0)
    A, B, d = plant.linearised_system(T_outdoor=5.0, T_rad_op=30.0, dt_s=3600.0)

    assert np.all(np.isfinite(A))
    assert np.all(np.isfinite(B))
    assert np.all(np.isfinite(d))


def test_linearisation_stable_eigenvalues() -> None:
    """The linearised plant has all eigenvalues inside the unit circle."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    A, _, _ = plant.linearised_system(T_outdoor=5.0, T_rad_op=30.0)
    eigs = np.linalg.eigvals(A)
    # All eigenvalues inside the unit circle ⇒ stable open-loop plant.
    assert max(abs(eigs)) < 1.0


def _composed_by_substeps(
    plant: PlantModelRC2, T_outdoor: float, T_rad_op: float, n_steps: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(A, B, d)`` of ``n_steps`` nominal steps, composed one by one."""
    A_step, B_step, d_step = plant.linearised_system(T_outdoor, T_rad_op)
    A = np.eye(2)
    B = np.zeros((2, 1))
    d = np.zeros(2)
    for _ in range(n_steps):
        B = A_step @ B + B_step
        d = A_step @ d + d_step
        A = A_step @ A
    return A, B, d


@pytest.mark.parametrize("n_steps", [1, 2, 7, 120, 1000])
def test_a_long_linearised_interval_equals_its_composed_substeps(n_steps: int) -> None:
    """Covering ``n`` nominal steps at once is the product of ``n`` single steps."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)

    A, B, d = plant.linearised_system(
        T_outdoor=5.0, T_rad_op=35.0, dt_s=n_steps * plant.dt_s
    )

    A_ref, B_ref, d_ref = _composed_by_substeps(plant, 5.0, 35.0, n_steps)
    np.testing.assert_allclose(A, A_ref, rtol=1e-9, atol=1e-12)
    np.testing.assert_allclose(B, B_ref, rtol=1e-9, atol=1e-12)
    np.testing.assert_allclose(d, d_ref, rtol=1e-9, atol=1e-12)


@pytest.mark.parametrize("n_steps", [1, 2, 7, 120, 1000])
def test_a_long_propagation_equals_its_euler_substeps(n_steps: int) -> None:
    """Propagating over ``n`` nominal steps lands where ``n`` Euler steps do."""
    plant = PlantModelRC2(PlantParams(), dt_s=30.0)
    x0 = np.array([19.0, 24.0])

    x = plant.propagate(x0, u=0.6, outdoor_temperature=5.0, dt_s=n_steps * plant.dt_s)

    reference = x0
    for _ in range(n_steps):
        reference = plant.discrete_step(reference, u=0.6, T_outdoor=5.0)
    np.testing.assert_allclose(x, reference, rtol=1e-9)


def _plant_lines_run(work: Callable[[], Any]) -> int:
    """Return how many lines of the plant module ``work`` executes."""
    plant_source = Path(_plant.__file__).resolve()
    count = 0

    def tracer(frame: FrameType, event: str, _arg: object) -> TraceFunction | None:
        nonlocal count
        if Path(frame.f_code.co_filename).resolve() != plant_source:
            return None
        if event == "line":
            count += 1
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        work()
    finally:
        sys.settrace(previous)
    return count


def test_a_month_long_gap_runs_no_more_plant_code_than_one_step() -> None:
    """The observer's cost for a sensor gap does not grow with the gap.

    The prediction runs on the event loop once per controller after every
    gap; a month without readings on a slow room spans tens of thousands of
    nominal steps, which must not mean as many passes through Python code.
    """
    plant = PlantModelRC2(PlantParams(tau_room_min=2000.0), dt_s=30.0)
    x0 = np.array([19.0, 24.0])
    month_s = 30 * 86_400.0
    assert math.ceil(min(month_s, plant.settling_time_s) / plant.dt_s) > 50_000

    def predict(dt_s: float) -> None:
        plant.linearised_system(T_outdoor=5.0, T_rad_op=24.0, dt_s=dt_s)
        plant.propagate(x0, u=0.5, outdoor_temperature=5.0, dt_s=dt_s)

    assert _plant_lines_run(lambda: predict(month_s)) == _plant_lines_run(
        lambda: predict(plant.dt_s)
    )
