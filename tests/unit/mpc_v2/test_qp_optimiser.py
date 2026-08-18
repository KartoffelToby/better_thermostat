"""Unit tests for the MPC v2 QP optimiser and portable fallback."""

from __future__ import annotations

import numpy as np

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


def test_box_constraint_clamps_to_u_max() -> None:
    """The box constraint keeps the command at or below u_max."""
    opt = _make_optimiser()
    x_pred = np.array([10.0, 10.0])
    u = opt.solve(x_pred, T_sp=25.0, T_outdoor_C=-20.0, u_last=1.0)
    assert u <= 1.0 + 1e-6


def test_anti_windup_skips_saturated_integration() -> None:
    """Integration must skip when u is pinned *against* the sign of the error."""
    # Mid-rail u — always integrates.
    opt = _make_optimiser()
    opt.update_integral(T_room=21.0, T_sp=22.0, u_applied=0.5, dt_s=300.0)
    assert opt.e_integral_K_min < 0.0  # err = -1, dt = 5 min ⇒ −5 K·min

    # u = u_max with T_room < T_sp (we want more heat but valve already pinned
    # open against an err that would only grow the negative integrator).
    opt.reset_integral()
    opt.update_integral(T_room=21.0, T_sp=22.0, u_applied=1.0, dt_s=300.0)
    assert opt.e_integral_K_min == 0.0

    # u = u_min with T_room > T_sp (valve closed, can't cool faster, positive
    # err would push the integrator up — skip).
    opt.reset_integral()
    opt.update_integral(T_room=23.0, T_sp=22.0, u_applied=0.0, dt_s=300.0)
    assert opt.e_integral_K_min == 0.0


def test_integral_clipping() -> None:
    """The error integrator is clipped to its configured magnitude."""
    opt = _make_optimiser()
    opt.params.integral_clip_K_min = 5.0
    # Hammer the integrator: T_room - T_sp = 10 K, dt = 5 min, 100 times.
    for _ in range(100):
        opt.update_integral(T_room=30.0, T_sp=20.0, u_applied=0.5, dt_s=300.0)
    assert abs(opt.e_integral_K_min) <= 5.0 + 1e-6


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
