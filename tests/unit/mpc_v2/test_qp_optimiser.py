"""Unit tests for the MPC v2 QP optimiser and portable fallback."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.plant import (
    PlantModelRC2,
    PlantParams,
)
from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.qp_optimiser import (
    QpOptimiser,
    QpParams,
)


def _make_optimiser(delta_u_max: float = 1.0) -> QpOptimiser:
    """Build a QP optimiser with the given per-step ramp limit."""
    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    return QpOptimiser(plant, QpParams(delta_u_max=delta_u_max))


def test_cold_room_commands_heat() -> None:
    """A cold room below target commands a substantial heat call."""
    opt = _make_optimiser()
    x_pred = np.array([18.0, 18.0])
    u = opt.solve(x_pred, T_sp=22.0, T_outdoor_C=5.0, u_last=0.0)
    assert u > 0.1, f"expected substantial heat call, got u={u}"


@pytest.mark.parametrize("T_sp", [25.0, 30.0])
def test_cold_room_below_a_setpoint_beyond_the_water_still_commands_heat(
    T_sp: float,
) -> None:
    """A setpoint whose steady radiator lies above the supply water still heats.

    At -16 °C a large room needs a radiator of ``2·T_sp + 16`` °C, beyond the
    65 °C water for both setpoints. The valve can still only warm the
    radiator, so a room 3 K below the setpoint gets the full first ramp.
    """
    plant_params = PlantParams(tau_room_min=720.0)
    opt = QpOptimiser(PlantModelRC2(plant_params, dt_s=300.0), QpParams())
    assert opt.plant.steady_radiator_temp(T_sp, -16.0) > plant_params.T_water_C

    u = opt.solve(np.array([T_sp - 3.0, T_sp - 3.0]), T_sp, -16.0, u_last=0.0)

    assert u == pytest.approx(QpParams().delta_u_max)


def test_warm_room_above_target_commands_zero() -> None:
    """A room above target commands little to no heat."""
    opt = _make_optimiser()
    x_pred = np.array([24.0, 35.0])
    u = opt.solve(x_pred, T_sp=22.0, T_outdoor_C=5.0, u_last=0.5)
    assert u < 0.2


def test_delta_u_constraint_clamps_first_step() -> None:
    """The delta-u constraint clamps how far the first command can move."""
    opt = _make_optimiser(delta_u_max=0.05)
    x_pred = np.array([15.0, 15.0])
    u = opt.solve(x_pred, T_sp=22.0, T_outdoor_C=-10.0, u_last=0.0)
    assert 0.0 <= u <= 0.05 + 1e-6


@pytest.mark.parametrize("solver", ["daqp", "portable"])
def test_box_constraint_bounds_the_whole_horizon(monkeypatch, solver) -> None:
    """Every planned step stays inside the valve box, and a cold room reaches it.

    The first command is clamped once more on its way out, so only the
    planned trajectory shows whether the optimiser itself honours the box.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    plans: list[np.ndarray] = []
    if solver == "daqp":
        if not qp_optimiser.DAQP_AVAILABLE or qp_optimiser._daqp is None:
            pytest.skip("the daqp solver is not installed")
        real_solve = qp_optimiser._daqp.solve

        def _recording_solve(*args):
            result = real_solve(*args)
            plans.append(np.asarray(result[0], dtype=float))
            return result

        monkeypatch.setattr(
            qp_optimiser, "_daqp", SimpleNamespace(solve=_recording_solve)
        )
    else:
        monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
        monkeypatch.setattr(qp_optimiser, "_daqp", None)
        real_descent = qp_optimiser.QpOptimiser._solve_coordinate_descent

        def _recording_descent(self, *args):
            result = real_descent(self, *args)
            plans.append(np.asarray(result, dtype=float))
            return result

        monkeypatch.setattr(
            qp_optimiser.QpOptimiser, "_solve_coordinate_descent", _recording_descent
        )

    plant = PlantModelRC2(PlantParams(), dt_s=300.0)
    opt = QpOptimiser(plant, QpParams(delta_u_max=1.0, u_max=0.6))
    opt.solve(np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=5.0, u_last=0.6)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.max() <= 0.6 + 1e-6, plan
    assert plan.min() >= -1e-6, plan
    assert plan[0] == pytest.approx(0.6, abs=1e-6), plan


def test_anti_windup_skips_saturated_integration() -> None:
    """Integration must skip when u is pinned *against* the sign of the error."""
    # Mid-rail u — always integrates.
    opt = _make_optimiser()
    opt.update_integral(T_room=21.6, T_sp=22.0, u_applied=0.5, dt_s=300.0)
    assert opt.e_integral_K_min == pytest.approx(-2.0)  # err = -0.4, dt = 5 min

    # u = u_max with T_room < T_sp (we want more heat but valve already pinned
    # open against an err that would only grow the negative integrator).
    opt.reset_integral()
    opt.update_integral(T_room=21.6, T_sp=22.0, u_applied=1.0, dt_s=300.0)
    assert opt.e_integral_K_min == 0.0

    # u = u_min with T_room > T_sp (valve closed, can't cool faster, positive
    # err would push the integrator up — skip).
    opt.reset_integral()
    opt.update_integral(T_room=22.4, T_sp=22.0, u_applied=0.0, dt_s=300.0)
    assert opt.e_integral_K_min == 0.0


@pytest.mark.parametrize(
    ("T_room", "expected_K_min"), [(21.4, 0.0), (21.6, -2.0), (22.4, 2.0), (22.6, 0.0)]
)
def test_integral_collects_only_errors_inside_the_band(
    T_room: float, expected_K_min: float
) -> None:
    """Errors beyond ``integral_error_band_K`` leave the integral untouched.

    A room 0.6 K off the setpoint is still being driven there by the plan,
    while 0.4 K counts as residual offset. The valve sits mid-rail, so only
    the band decides.
    """
    opt = _make_optimiser()
    assert opt.params.integral_error_band_K == 0.5

    opt.update_integral(T_room=T_room, T_sp=22.0, u_applied=0.5, dt_s=300.0)

    assert opt.e_integral_K_min == pytest.approx(expected_K_min)


def test_integral_clipping() -> None:
    """The error integrator is clipped to its configured magnitude."""
    opt = _make_optimiser()
    opt.params.integral_clip_K_min = 5.0
    # Hammer the integrator: T_room - T_sp = 0.4 K, dt = 5 min, 100 times.
    for _ in range(100):
        opt.update_integral(T_room=20.4, T_sp=20.0, u_applied=0.5, dt_s=300.0)
    assert opt.e_integral_K_min == pytest.approx(5.0)


def test_numpy_fallback_obeys_constraints(monkeypatch) -> None:
    """The NumPy fallback remains usable and rate-limited without DAQP."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_daqp", None)
    opt = _make_optimiser(delta_u_max=0.05)
    u = opt.solve(np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=-10.0, u_last=0.0)
    assert 0.0 <= u <= 0.05 + 1e-6
    assert u > 0.0
