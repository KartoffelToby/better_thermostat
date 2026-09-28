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


@pytest.mark.parametrize("target_temperature", [25.0, 30.0])
def test_cold_room_below_a_setpoint_beyond_the_water_still_commands_heat(
    target_temperature: float,
) -> None:
    """A setpoint whose steady radiator lies above the supply water still heats.

    At -16 °C a large room needs a radiator of ``2·T_sp + 16`` °C, beyond the
    65 °C water for both setpoints. The valve can still only warm the
    radiator, so a room 3 K below the setpoint gets the full first ramp.
    """
    plant_params = PlantParams(tau_room_min=720.0)
    opt = QpOptimiser(PlantModelRC2(plant_params, dt_s=300.0), QpParams())
    target = target_temperature
    assert opt.plant.steady_radiator_temp(target, -16.0) > plant_params.T_water_C

    u = opt.solve(np.array([target - 3.0, target - 3.0]), target, -16.0, u_last=0.0)

    assert u == pytest.approx(QpParams().delta_u_max)


@pytest.mark.parametrize("target_temperature", [25.0, 30.0])
def test_warm_room_above_a_setpoint_beyond_the_water_backs_the_valve_off(
    target_temperature: float,
) -> None:
    """A room 2 K above a setpoint the radiator cannot hold gets less heat.

    The radiator already sits at the hottest a fully open valve holds it, so
    opening further cannot help and the room has margin to fall; the plan
    closes the valve as far as the ramp limit allows. The optimiser must see
    the valve's full gain at that radiator temperature to decide this.
    """
    plant_params = PlantParams(tau_room_min=720.0)
    opt = QpOptimiser(PlantModelRC2(plant_params, dt_s=300.0), QpParams())
    target = target_temperature
    hottest = opt.plant.hottest_radiator_temp(target)
    assert opt.plant.steady_radiator_temp(target, -16.0) > plant_params.T_water_C

    u = opt.solve(np.array([target + 2.0, hottest]), target, -16.0, u_last=0.5)

    assert u == pytest.approx(0.5 - QpParams().delta_u_max)


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
        real_portable = qp_optimiser.QpOptimiser._solve_portable

        def _recording_portable_plan(self, *args):
            result = real_portable(self, *args)
            plans.append(np.asarray(result, dtype=float))
            return result

        monkeypatch.setattr(
            qp_optimiser.QpOptimiser, "_solve_portable", _recording_portable_plan
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
    ("room_temperature", "expected_integral"),
    [(21.4, 0.0), (21.6, -2.0), (22.4, 2.0), (22.6, 0.0)],
)
def test_integral_collects_only_errors_inside_the_band(
    room_temperature: float, expected_integral: float
) -> None:
    """Errors beyond ``integral_error_band`` leave the integral untouched.

    A room 0.6 K off the setpoint is still being driven there by the plan,
    while 0.4 K counts as residual offset. The valve sits mid-rail, so only
    the band decides.
    """
    opt = _make_optimiser()
    assert opt.params.integral_error_band == 0.5

    opt.update_integral(T_room=room_temperature, T_sp=22.0, u_applied=0.5, dt_s=300.0)

    assert opt.e_integral_K_min == pytest.approx(expected_integral)


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


# Operating states drawn per spread: (e_integral, outdoor, setpoint offset
# range of the room, radiator above room, disturbance) bounds. "extreme"
# covers every room the controller can meet, including setpoints far out of
# reach and a radiator at supply-water temperature.
_STATE_SPREADS = {
    "realistic": ((-5, 5), (-5, 12), (-0.5, 0.3), (2, 20), 0.01),
    "wide": ((-60, 60), (-15, 18), (-3, 2), (0, 40), 0.05),
    "extreme": ((-60, 60), (-25, 25), (-8, 5), (-2, 60), 0.2),
}


@pytest.mark.parametrize("spread", list(_STATE_SPREADS))
def test_portable_solver_commands_the_valve_daqp_commands(
    monkeypatch, spread: str
) -> None:
    """Without daqp the valve command equals daqp's to 0.0001 percentage point.

    Both solve the same convex QP, whose optimum is unique; the states cover
    tight and loose rate limits, a capped valve and every plant speed.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    if not qp_optimiser.DAQP_AVAILABLE or qp_optimiser._daqp is None:
        pytest.skip("the daqp solver is not installed")
    integral, outdoor, room_offset, radiator_above, disturbance = _STATE_SPREADS[spread]
    rng = np.random.default_rng(42)
    worst = 0.0
    for _ in range(300):
        tau = float(rng.uniform(60.0, 1500.0))
        plant = PlantModelRC2(
            PlantParams(tau_room_min=tau), dt_s=float(rng.uniform(60.0, 600.0))
        )
        opt = QpOptimiser(
            plant,
            QpParams(
                delta_u_max=float(rng.choice([0.05, 0.2, 0.45, 1.0])),
                u_max=float(rng.choice([0.3, 1.0])),
            ),
        )
        opt.e_integral_K_min = float(rng.uniform(*integral))
        t_sp = float(rng.uniform(5.0, 30.0))
        t_room = t_sp + float(rng.uniform(*room_offset))
        x = np.array([t_room, t_room + float(rng.uniform(*radiator_above))])
        args = (
            x,
            t_sp,
            float(rng.uniform(*outdoor)),
            float(rng.uniform(0.0, 1.0)),
            float(rng.uniform(-disturbance, disturbance)),
        )
        monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", True)
        with_daqp = opt.solve(*args)
        monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
        portable = opt.solve(*args)
        worst = max(worst, abs(with_daqp - portable))
    assert worst <= 1e-6, f"{100 * worst:.6f} percentage points apart"


def test_missing_daqp_is_logged_once_per_optimiser(monkeypatch, caplog) -> None:
    """A controller built without daqp says in the log which solver plans."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_daqp", None)
    caplog.set_level("INFO", logger=qp_optimiser.__name__)
    opt = _make_optimiser()
    for _ in range(3):
        opt.solve(np.array([19.0, 30.0]), T_sp=21.0, T_outdoor_C=0.0, u_last=0.3)

    notes = [r for r in caplog.records if "daqp" in r.getMessage()]
    assert len(notes) == 1
    assert notes[0].levelname == "INFO"
    assert "NumPy" in notes[0].getMessage()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param((np.zeros(12), 0.0, -1, {}), id="exit-flag"),
        pytest.param(ValueError("singular"), id="exception"),
    ],
)
def test_a_failing_daqp_solve_is_logged_once_and_the_plan_still_comes(
    monkeypatch, caplog, failure
) -> None:
    """A daqp failure warns once per optimiser and the NumPy solver plans instead."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    def _failing_solve(*_args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", True)
    monkeypatch.setattr(qp_optimiser, "_daqp", SimpleNamespace(solve=_failing_solve))
    caplog.set_level("DEBUG", logger=qp_optimiser.__name__)
    opt = _make_optimiser()
    commands = [
        opt.solve(np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=-10.0, u_last=0.3)
        for _ in range(3)
    ]

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    portable = _make_optimiser().solve(
        np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=-10.0, u_last=0.3
    )

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "daqp" in warnings[0].getMessage()
    assert commands == [pytest.approx(portable, abs=1e-12)] * 3


def test_portable_solver_holds_the_valve_on_a_non_finite_objective(monkeypatch) -> None:
    """A NaN in the plant state keeps the last command instead of a rail."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    opt = _make_optimiser(delta_u_max=0.45)
    u = opt.solve(
        np.array([float("nan"), 30.0]), T_sp=21.0, T_outdoor_C=0.0, u_last=0.4
    )
    assert u == pytest.approx(0.4)


@pytest.mark.parametrize("weights", ["default", "extreme"])
def test_portable_solver_plans_feasibly_and_like_daqp_for_any_plant_and_weights(
    monkeypatch, weights: str
) -> None:
    """Without daqp the whole plan stays feasible and its command equals daqp's.

    Plants span every time constant, gain and supply temperature the model
    admits, with the default cost weights or with weights far off them (no
    smoothing, a heavy integral, no effort term), and states far beyond the
    setpoint. The command must match daqp to 0.0001 percentage point wherever
    daqp returns a feasible optimum, and every planned step has to respect
    the valve box and the rate limit before any clamping on the way out.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    if not qp_optimiser.DAQP_AVAILABLE or qp_optimiser._daqp is None:
        pytest.skip("the daqp solver is not installed")
    plans: list[tuple[np.ndarray, float, float, float, float]] = []
    real_portable = qp_optimiser.QpOptimiser._solve_portable

    def _recording_portable(self, hessian, gradient, bounds):
        plan = real_portable(self, hessian, gradient, bounds)
        plans.append(
            (
                np.asarray(plan, dtype=float),
                bounds.u_last,
                bounds.u_min,
                bounds.u_max,
                bounds.delta_u_max,
            )
        )
        return plan

    monkeypatch.setattr(
        qp_optimiser.QpOptimiser, "_solve_portable", _recording_portable
    )
    rng = np.random.default_rng(3)
    worst_gap = 0.0
    worst_violation = 0.0
    compared = 0
    for _ in range(800):
        plant = PlantModelRC2(
            PlantParams(
                tau_room_min=float(rng.uniform(30.0, 3000.0)),
                tau_rad_min=float(rng.uniform(2.0, 60.0)),
                gain_heater=float(rng.uniform(0.2, 8.0)),
                coupling_rad_room=float(rng.uniform(0.2, 3.0)),
                T_water_C=float(rng.uniform(35.0, 80.0)),
            ),
            dt_s=300.0,
        )
        if weights == "default":
            params = QpParams(step_s=float(rng.choice([90.0, 150.0, 300.0])))
            integral = float(rng.choice([-60.0, 60.0, rng.uniform(-60.0, 60.0)]))
            u_last = float(rng.choice([0.0, 1.0, rng.uniform(0.0, 1.0)]))
            disturbance = float(rng.uniform(-1.0, 1.0))
        else:
            params = QpParams(
                w_integral=float(rng.choice([0.0, 1e-4, 10.0, 1e3])),
                w_smooth=float(rng.choice([0.0, 1e-3, 100.0])),
                w_effort=float(rng.choice([0.0, 1e-6, 1.0])),
            )
            integral = float(rng.uniform(-200.0, 200.0))
            u_last = float(rng.uniform(0.0, 1.0))
            disturbance = float(rng.uniform(-0.2, 0.2))
        opt = QpOptimiser(plant, params)
        opt.e_integral_K_min = integral
        args = (
            np.array([rng.uniform(5.0, 30.0), rng.uniform(5.0, 70.0)]),
            float(rng.uniform(5.0, 30.0)),
            float(rng.uniform(-25.0, 25.0)),
            u_last,
            disturbance,
        )
        daqp_plans: list[tuple[np.ndarray, int]] = []
        real_daqp_solve = qp_optimiser._daqp.solve

        def _recording_daqp(*daqp_args, _real=real_daqp_solve, _out=daqp_plans):
            result = _real(*daqp_args)
            _out.append((np.asarray(result[0], dtype=float), int(result[2])))
            return result

        monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", True)
        monkeypatch.setattr(
            qp_optimiser, "_daqp", SimpleNamespace(solve=_recording_daqp)
        )
        with_daqp = opt.solve(*args)
        monkeypatch.setattr(
            qp_optimiser, "_daqp", SimpleNamespace(solve=real_daqp_solve)
        )
        monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
        portable = opt.solve(*args)

        plan, last, low, high, rate = plans[-1]
        steps = np.diff(np.concatenate([[last], plan]))
        violation = max(
            float(np.max(plan - high)),
            float(np.max(low - plan)),
            float(np.max(np.abs(steps))) - rate,
        )
        worst_violation = max(worst_violation, violation)
        daqp_plan, exit_flag = daqp_plans[-1]
        daqp_steps = np.diff(np.concatenate([[last], daqp_plan]))
        daqp_violation = max(
            float(np.max(daqp_plan - high)),
            float(np.max(low - daqp_plan)),
            float(np.max(np.abs(daqp_steps))) - rate,
        )
        if exit_flag == 1 and daqp_violation <= 1e-7:
            compared += 1
            worst_gap = max(worst_gap, abs(with_daqp - portable))

    assert worst_violation <= 1e-9
    assert compared >= 700
    assert worst_gap <= 1e-6, f"{100 * worst_gap:.6f} percentage points apart"


def test_portable_solver_plans_flat_and_says_so_when_it_does_not_converge(
    monkeypatch, caplog
) -> None:
    """An unconverged solve yields the best feasible flat plan and says so."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_INTERIOR_POINT_ITERATIONS", 1)
    caplog.set_level("DEBUG", logger=qp_optimiser.__name__)
    plans: list[np.ndarray] = []
    real_portable = qp_optimiser.QpOptimiser._solve_portable

    def _recording(self, *args):
        plan = real_portable(self, *args)
        plans.append(np.asarray(plan, dtype=float))
        return plan

    monkeypatch.setattr(qp_optimiser.QpOptimiser, "_solve_portable", _recording)
    opt = _make_optimiser(delta_u_max=0.2)
    u = opt.solve(np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=-10.0, u_last=0.3)

    (plan,) = plans
    assert np.all(plan == plan[0])
    assert u == pytest.approx(0.5)
    assert any("planning flat" in r.getMessage() for r in caplog.records)


def test_portable_solver_is_exact_on_arbitrary_convex_plans() -> None:
    """Any convex plan objective is solved feasibly and to daqp's optimum.

    Horizons of 1 to 24 steps, Hessians from well conditioned to nearly
    rank one, gradients over seven orders of magnitude, rate limits from
    zero to wider than the valve and a last command outside the valve range.
    The objective may exceed daqp's by a millionth of its size.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals.qp_optimiser import (
        _SolverBounds,
    )

    if not qp_optimiser.DAQP_AVAILABLE or qp_optimiser._daqp is None:
        pytest.skip("the daqp solver is not installed")
    rng = np.random.default_rng(1)
    opt = QpOptimiser(PlantModelRC2(PlantParams(), dt_s=300.0), QpParams())
    worst_gap = 0.0
    worst_violation = 0.0
    compared = 0
    for _ in range(1500):
        n = int(rng.integers(1, 25))
        opt.N = n
        m = rng.normal(size=(n, n)) * rng.choice([1e-4, 1.0, 1e3])
        shape = rng.choice(["definite", "ill", "rank-one"])
        if shape == "definite":
            hessian = m @ m.T + np.eye(n) * rng.choice([1e-6, 1e-2, 1.0])
        elif shape == "ill":
            hessian = m @ m.T + np.eye(n) * 1e-10
        else:
            v = rng.normal(size=n)
            hessian = np.outer(v, v) * 1e3 + np.eye(n) * 1e-8
        gradient = rng.normal(size=n) * rng.choice([1e-3, 1.0, 1e4])
        bounds = _SolverBounds(
            u_last=float(rng.choice([0.0, 1.0, rng.uniform(-0.2, 1.2)])),
            u_min=0.0,
            u_max=1.0,
            delta_u_max=float(rng.choice([0.0, 1e-9, 0.05, 0.3, 2.0])),
        )
        first_lo = max(bounds.u_min, bounds.u_last - bounds.delta_u_max)
        first_hi = min(bounds.u_max, bounds.u_last + bounds.delta_u_max)
        if first_lo > first_hi:
            continue
        plan = opt._solve_portable(hessian, gradient, bounds)

        rise = np.eye(n) - np.eye(n, k=-1)
        offset = np.zeros(n)
        offset[0] = bounds.u_last
        steps = rise @ plan - offset
        worst_violation = max(
            worst_violation,
            float(np.max(plan - 1.0)),
            float(np.max(-plan)),
            float(np.max(np.abs(steps))) - bounds.delta_u_max,
        )
        reference, _, exit_flag, _ = qp_optimiser._daqp.solve(
            hessian,
            gradient,
            np.vstack([np.eye(n), rise]),
            np.concatenate([np.ones(n), bounds.delta_u_max + offset]),
            np.concatenate([np.zeros(n), -bounds.delta_u_max + offset]),
            np.zeros(2 * n, dtype=np.int32),
        )
        reference_steps = rise @ reference - offset
        reference_violation = max(
            float(np.max(reference - 1.0)),
            float(np.max(-reference)),
            float(np.max(np.abs(reference_steps))) - bounds.delta_u_max,
        )
        objective = 0.5 * plan @ hessian @ plan + gradient @ plan
        optimum = 0.5 * reference @ hessian @ reference + gradient @ reference
        # A rate limit within the feasibility tolerance admits only flat
        # plans, up to steps of that tolerance; only feasibility counts there.
        if exit_flag == 1 and reference_violation <= 1e-9 and bounds.delta_u_max > 1e-9:
            compared += 1
            worst_gap = max(worst_gap, (objective - optimum) / max(1.0, abs(optimum)))

    assert worst_violation <= 1e-9
    assert compared >= 450
    assert worst_gap <= 1e-6


def test_portable_solver_never_returns_an_infeasible_plan(monkeypatch, caplog) -> None:
    """A plan that breaks a limit is replaced by the best flat plan."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(
        qp_optimiser.QpOptimiser,
        "_interior_point",
        staticmethod(lambda hessian, *_args: np.full(len(hessian), 2.0)),
    )
    caplog.set_level("DEBUG", logger=qp_optimiser.__name__)
    opt = _make_optimiser(delta_u_max=0.2)

    u = opt.solve(np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=-10.0, u_last=0.3)

    assert u == pytest.approx(0.5)
    assert any("planning flat" in r.getMessage() for r in caplog.records)


def test_portable_solver_finishes_a_plan_whose_last_newton_matrix_breaks_down(
    monkeypatch,
) -> None:
    """A nearly converged plan is finished, not dropped for a flat one.

    The weak radiator and tight coupling here drive the Newton matrix to
    lose definiteness in the last iterations, when the plan is already all
    but optimal; the finished plan equals daqp's.
    """
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    if not qp_optimiser.DAQP_AVAILABLE or qp_optimiser._daqp is None:
        pytest.skip("the daqp solver is not installed")
    plant = PlantModelRC2(
        PlantParams(
            tau_room_min=30.0,
            tau_rad_min=8.027735022147063,
            gain_heater=0.05,
            coupling_rad_room=10.0,
            T_water_C=25.0,
        ),
        300.0,
    )
    plans: dict[str, np.ndarray] = {}
    real_portable = qp_optimiser.QpOptimiser._solve_portable
    real_daqp_solve = qp_optimiser._daqp.solve

    def _recording_portable(self, *args):
        plans["portable"] = np.asarray(real_portable(self, *args), dtype=float)
        return plans["portable"]

    def _recording_daqp(*args):
        result = real_daqp_solve(*args)
        plans["daqp"] = np.asarray(result[0], dtype=float)
        return result

    monkeypatch.setattr(
        qp_optimiser.QpOptimiser, "_solve_portable", _recording_portable
    )
    monkeypatch.setattr(qp_optimiser, "_daqp", SimpleNamespace(solve=_recording_daqp))
    args = (
        np.array([36.0637556, 34.79238931]),
        9.273441583115412,
        12.462598956697983,
        0.55,
        -0.16768932240961143,
    )
    for daqp_available in (True, False):
        monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", daqp_available)
        opt = QpOptimiser(plant, QpParams(delta_u_max=0.25))
        opt.e_integral_K_min = 33.99403235665278
        opt.solve(*args)

    assert np.max(np.abs(plans["portable"] - plans["daqp"])) <= 1e-6


_NON_FINITE_STATES = {
    "room-nan": {"x_pred": np.array([float("nan"), 30.0])},
    "radiator-inf": {"x_pred": np.array([19.0, float("inf")])},
    "setpoint-nan": {"T_sp": float("nan")},
    "outdoor-nan": {"T_outdoor_C": float("nan")},
    "disturbance-inf": {"D_hat_K_per_min": float("inf")},
}


@pytest.mark.parametrize("solver", ["daqp", "portable"])
@pytest.mark.parametrize("state", list(_NON_FINITE_STATES))
def test_a_non_finite_plan_holds_the_last_command(
    monkeypatch, solver: str, state: str
) -> None:
    """A state that makes the plan non-finite keeps the valve where it was."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    if solver == "daqp" and (
        not qp_optimiser.DAQP_AVAILABLE or qp_optimiser._daqp is None
    ):
        pytest.skip("the daqp solver is not installed")
    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", solver == "daqp")
    arguments = {
        "x_pred": np.array([19.0, 30.0]),
        "T_sp": 21.0,
        "T_outdoor_C": 0.0,
        "u_last": 0.4,
        "D_hat_K_per_min": 0.0,
    }
    arguments.update(_NON_FINITE_STATES[state])

    assert _make_optimiser(delta_u_max=0.45).solve(**arguments) == pytest.approx(0.4)


def test_a_repeated_flat_fallback_warns_once(monkeypatch, caplog) -> None:
    """The first plan the NumPy solver cannot finish warns, later ones do not."""
    from custom_components.better_thermostat.utils.calibration.mpc_v2_internals import (
        qp_optimiser,
    )

    monkeypatch.setattr(qp_optimiser, "DAQP_AVAILABLE", False)
    monkeypatch.setattr(qp_optimiser, "_INTERIOR_POINT_ITERATIONS", 1)
    caplog.set_level("DEBUG", logger=qp_optimiser.__name__)
    opt = _make_optimiser(delta_u_max=0.2)
    for _ in range(3):
        opt.solve(np.array([15.0, 15.0]), T_sp=22.0, T_outdoor_C=-10.0, u_last=0.3)

    flat = [r for r in caplog.records if "planning flat" in r.getMessage()]
    assert [r.levelname for r in flat] == ["WARNING", "DEBUG", "DEBUG"]
