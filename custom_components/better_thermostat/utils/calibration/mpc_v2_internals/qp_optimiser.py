"""Finite-horizon QP MPC with an optional DAQP accelerator.

Solves over ``u = [u_0, …, u_{N-1}]``:

    J = Σ_k  w_c·(T_room_k − T_sp)²
            + w_e·(u_k − u_ss)²
            + w_du·(u_k − u_{k-1})²
            + w_i·(I_k − 0)²

where ``I_k`` is the predicted integrated tracking error. Hard constraints
``u ∈ [u_min, u_max]`` and ``|Δu| ≤ Δu_max`` are encoded directly.

DAQP (dense active-set) is used when its native wheel is available.  Home
Assistant's Alpine-based images cannot install that wheel on every supported
architecture, so a small interior-point solver using only NumPy finds the
same optimum under the same hard valve constraints everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import math
from types import ModuleType

import numpy as np

from ._types import FloatArray
from .plant import PlantModelRC2

_LOGGER = logging.getLogger(__name__)


def _try_import_daqp() -> ModuleType | None:
    """Return the optional DAQP module, or ``None`` when it is unavailable.

    ``daqp`` is an optional dependency — it is not a manifest requirement
    (it has no aarch64 wheel for the HA Python) and may be absent, and it ships
    no type stubs; importing it by name via ``importlib`` keeps the static
    checkers from resolving (and flagging) it. The wrapping function also
    avoids ``reportConstantRedefinition`` on the module-level result flags,
    which are written once at import.
    """
    try:
        return importlib.import_module("daqp")
    except ImportError:  # pragma: no cover — depends on the active platform
        return None


_daqp = _try_import_daqp()
DAQP_AVAILABLE = _daqp is not None


# Interior-point budget and tolerances on the scaled problem. The method
# converges in 10 to 40 iterations on every plan the tests draw; the budget
# only bounds a pathological case. A plan step beyond ``_FEASIBILITY_TOL``
# of a limit counts as infeasible.
_INTERIOR_POINT_ITERATIONS = 100
_CONVERGENCE_TOL = 1e-11
_FEASIBILITY_TOL = 1e-9


def _bounded_command(
    command: float, u_last: float, u_min: float, u_max: float
) -> float:
    """Return the first planned command inside the valve range.

    A non-finite command, from a state the plan cannot handle, keeps the
    valve at the last command rather than letting the clamps open it fully.
    """
    if not math.isfinite(command):
        command = u_last if math.isfinite(u_last) else u_min
    return max(u_min, min(u_max, command))


def _step_to_boundary(values: FloatArray, change: FloatArray) -> float:
    """Return the largest step in ``(0, 1]`` that keeps ``values`` non-negative."""
    shrinking = change < 0.0
    if not np.any(shrinking):
        return 1.0
    return min(1.0, float(np.min(-values[shrinking] / change[shrinking])))


def _newton_direction(
    factor: FloatArray,
    rows: FloatArray,
    slack: FloatArray,
    dual: FloatArray,
    residuals: tuple[FloatArray, FloatArray],
    centring: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Return the interior-point step in ``x``, the slacks and the multipliers.

    ``factor`` is the Cholesky factor of the reduced Newton matrix
    ``H + Cᵀ·diag(dual/slack)·C``; ``centring`` is the complementarity
    target the step aims at.
    """
    dual_residual, primal_residual = residuals
    weight = dual / slack
    rhs = -dual_residual - rows.T @ (weight * primal_residual - centring / slack)
    dx = np.linalg.solve(factor.T, np.linalg.solve(factor, rhs))
    d_dual = weight * (rows @ dx + primal_residual) - centring / slack
    d_slack = -(centring + slack * d_dual) / dual
    return dx, d_slack, d_dual


def _solve_on_active(
    hessian: FloatArray, gradient: FloatArray, normals: FloatArray, targets: FloatArray
) -> tuple[FloatArray, FloatArray] | None:
    """Return the minimiser on ``normals·x = targets`` and its multipliers.

    The plan is split along a QR factorisation of ``normalsᵀ``: the part in
    the span of the normals follows from the targets alone, the rest from
    the objective restricted to the null space. Solving the plan and the
    multipliers as one system would tie the plan's round-off to the
    multipliers, which reach 1e11 when the gradient dwarfs the Hessian, and
    leave a vertex plan 1e-5 inside its constraints. Linearly dependent
    normals, or a Hessian singular on the null space, return ``None``.
    """
    size = hessian.shape[0]
    count = normals.shape[0]
    if count == 0:
        try:
            return np.linalg.solve(hessian, -gradient), np.zeros(0)
        except np.linalg.LinAlgError:
            return None
    if count > size:
        return None
    basis, triangle = np.linalg.qr(normals.T, mode="complete")
    diagonal = np.abs(np.diag(triangle[:count]))
    if float(np.min(diagonal)) <= size * np.finfo(float).eps * float(np.max(diagonal)):
        return None
    range_basis = basis[:, :count]
    null_basis = basis[:, count:]
    upper = triangle[:count]
    particular = range_basis @ np.linalg.solve(upper.T, targets)
    exact = particular
    if count < size:
        reduced = null_basis.T @ hessian @ null_basis
        try:
            free = np.linalg.solve(
                reduced, -null_basis.T @ (gradient + hessian @ particular)
            )
        except np.linalg.LinAlgError:
            return None
        exact = particular + null_basis @ free
    multipliers = np.linalg.solve(upper, -range_basis.T @ (gradient + hessian @ exact))
    return exact, multipliers


def _polish(
    hessian: FloatArray,
    gradient: FloatArray,
    rows: FloatArray,
    limits: FloatArray,
    slack: FloatArray,
    dual: FloatArray,
) -> FloatArray | None:
    """Solve exactly on the constraints the interior point ends on.

    A constraint starts out active where its multiplier exceeds its slack.
    A constraint the exact solution breaks joins the set, and the one with
    the most negative multiplier leaves it, for twice as many rounds as
    there are constraints. The result is returned only once it is feasible
    and every multiplier is non-negative, which makes it the optimum;
    otherwise ``None``.
    """
    active = [int(i) for i in np.flatnonzero(dual > slack)]
    for _ in range(2 * rows.shape[0]):
        count = len(active)
        solved = _solve_on_active(hessian, gradient, rows[active], limits[active])
        if solved is None:
            return None
        exact, multipliers = solved
        excess = rows @ exact - limits
        worst = int(np.argmax(excess))
        if excess[worst] > _FEASIBILITY_TOL:
            if worst in active:
                return None
            active.append(worst)
            continue
        largest = max(1.0, float(np.max(np.abs(multipliers)))) if count else 1.0
        if count and float(np.min(multipliers)) < -_FEASIBILITY_TOL * largest:
            del active[int(np.argmin(multipliers))]
            continue
        return exact
    return None


@dataclass
class QpParams:
    """Tunables for the finite-horizon QP MPC (weights, bounds, step sizing)."""

    # Key levers: a rate limit large enough for the valve to track the
    # setpoint, and a small integral weight — the feed-forward ``u_ss`` carries
    # steady state, so heavy integral action only adds overshoot.
    horizon_steps: int = 12
    # MPC re-plan cadence (seconds). Overridden by the adaptive_step logic
    # below unless ``adaptive_step_s`` is disabled.
    step_s: float = 300.0
    w_comfort: float = 140.0
    w_effort: float = 0.03
    w_smooth: float = 43.0
    w_integral: float = 0.02
    u_min: float = 0.0
    u_max: float = 1.0
    delta_u_max: float = 0.45
    integral_clip_K_min: float = 60.0
    # Anti-windup saturation band: when ``u`` sits within this distance of
    # ``u_min`` / ``u_max`` we treat it as saturated and skip the integral
    # update if the error sign would only grow it.
    saturation_band: float = 1e-3
    # Conditional integration: the integral only collects tracking errors
    # within this band (K). Larger errors belong to a setpoint ramp or a
    # recovery the plan already drives at full effort, and integrating them
    # leaves an overshoot that takes hours to unwind.
    integral_error_band: float = 0.5
    # Plant-aware step-size scaling. With ``adaptive_step_s=True`` the
    # ``step_s`` field above is recomputed at controller construction as
    # ``clamp(min, max, tau_room · per_tau)`` so fast envelopes get a finer
    # prediction grid and slow envelopes reuse the default ~300 s.
    adaptive_step_s: bool = True
    adaptive_step_s_per_tau: float = 0.6
    adaptive_step_s_min: float = 90.0
    adaptive_step_s_max: float = 300.0


@dataclass(frozen=True)
class _SolverBounds:
    """Hard valve constraints passed to an optimiser backend."""

    u_last: float
    u_min: float
    u_max: float
    delta_u_max: float


class QpOptimiser:
    """Receding-horizon optimiser returning the next valve fraction."""

    def __init__(self, plant: PlantModelRC2, params: QpParams) -> None:
        """Bind the plant and weights and precompute horizon helpers.

        Caches the horizon length ``N``, zeroes the integral tracking error,
        and builds the lower-triangular cumulative-sum matrix used to form the
        predicted integral term.

        Parameters
        ----------
        plant : PlantModelRC2
            RC2 plant providing the linearised prediction matrices.
        params : QpParams
            Cost weights, input bounds, and step-sizing tunables for the QP.
        """
        self.plant = plant
        self.params = params
        self.N = params.horizon_steps
        self.e_integral_K_min: float = 0.0
        self._L_cumsum = np.tril(np.ones((self.N, self.N)))
        # A daqp failure and a flat fallback of the NumPy solver are each
        # reported once per optimiser at WARNING, later ones at DEBUG, so a
        # solver that keeps failing does not flood the log.
        self._daqp_failure_reported = False
        self._flat_fallback_reported = False
        self._non_finite_reported = False
        if not DAQP_AVAILABLE:
            _LOGGER.info(
                "MPC v2 plans with its NumPy solver; the daqp package is not "
                "installed on this system"
            )

    def reset_integral(self) -> None:
        """Clear the accumulated integral tracking error."""
        self.e_integral_K_min = 0.0

    def update_integral(
        self, T_room: float, T_sp: float, u_applied: float, dt_s: float
    ) -> None:
        """Accumulate the tracking error with anti-windup and clipping.

        Skips accumulation when the applied input is saturated and the error
        sign would only grow the integral further, or when the error lies
        outside ``integral_error_band``. The interval counts at most one
        re-plan step: time without a plan is not tracking error. The running
        total is clipped to ``±integral_clip_K_min``.
        """
        err = T_room - T_sp
        if abs(err) > self.params.integral_error_band:
            return
        band = self.params.saturation_band
        at_upper = u_applied >= self.params.u_max - band
        at_lower = u_applied <= self.params.u_min + band
        if (at_upper and err < 0) or (at_lower and err > 0):
            return
        interval_s = min(max(0.0, dt_s), self.params.step_s)
        self.e_integral_K_min += (interval_s / 60.0) * err
        clip = self.params.integral_clip_K_min
        self.e_integral_K_min = max(-clip, min(clip, self.e_integral_K_min))

    def solve(
        self,
        x_pred: FloatArray,
        T_sp: float,
        T_outdoor: float,
        u_last: float,
        D_hat_K_per_min: float = 0.0,
    ) -> float:
        """Solve the horizon QP and return the first valve command ``u_0``.

        Builds the condensed prediction matrices from the linearised plant and
        assembles the Hessian and gradient. DAQP solves the small dense QP when
        available; otherwise a NumPy interior-point solver finds the same
        optimum under the same box bounds and rate limits.
        """
        n = self.plant.state_dim
        N = self.N

        # The operating point and the drift both carry the disturbance
        # estimate, so the prediction settles where ``u_ss`` holds the room.
        # A setpoint the radiator cannot hold puts the steady radiator above
        # what a fully open valve reaches, where the linearised valve gain
        # vanishes or turns negative; the hottest reachable radiator bounds it.
        # That bound keeps the gain positive only for a setpoint below the
        # supply water, which a room setpoint always is.
        radiator_operating_point = min(
            self.plant.steady_radiator_temp(T_sp, T_outdoor, D_hat_K_per_min),
            self.plant.hottest_radiator_temp(T_sp),
        )
        u_ss = self._steady_input_for(T_sp, T_outdoor, D_hat_K_per_min)
        A, B, d_vec = self.plant.linearised_AB(T_outdoor, radiator_operating_point)
        d_vec = d_vec + np.array([D_hat_K_per_min * self.plant.dt_min, 0.0])

        A_pow = [np.eye(n)]
        for _ in range(N):
            A_pow.append(A @ A_pow[-1])

        # Condense the prediction into lifted (stacked-over-horizon) maps from
        # the room-temperature output: ``Y_T_x`` is the free-response state map
        # (Aᵏ rows), ``Y_T_u`` the input→output step-response map (the
        # convolution of B through A), and ``Y_T_d`` the accumulated affine
        # drift term from the constant disturbance ``d_vec``.
        Y_T_x = np.zeros((N + 1, n))
        Y_T_u = np.zeros((N + 1, N))
        Y_T_d = np.zeros(N + 1)
        for k in range(N + 1):
            Y_T_x[k] = A_pow[k][0]
        for k in range(1, N + 1):
            d_sum = np.zeros(n)
            for i in range(k):
                Y_T_u[k, i] = float((A_pow[k - 1 - i] @ B).flatten()[0])
                d_sum = d_sum + A_pow[k - 1 - i] @ d_vec
            Y_T_d[k] = float(d_sum[0])

        x0 = x_pred[:n].astype(float)
        R_traj = np.full(N + 1, T_sp)
        track_const = Y_T_x @ x0 + Y_T_d - R_traj

        D_diff = np.eye(N)
        for k in range(1, N):
            D_diff[k, k - 1] = -1.0
        b_du = np.zeros(N)
        b_du[0] = u_last

        w_c = self.params.w_comfort
        w_e = self.params.w_effort
        w_du = self.params.w_smooth
        w_i = self.params.w_integral

        dt_min = self.plant.dt_min
        L = self._L_cumsum
        Y_T_x_N = Y_T_x[:N]
        Y_T_u_N = Y_T_u[:N]
        Y_T_d_N = Y_T_d[:N]
        I_T_u = dt_min * (L @ Y_T_u_N)
        I_const = self.e_integral_K_min * np.ones(N) + dt_min * (
            L @ (Y_T_x_N @ x0 + Y_T_d_N - T_sp)
        )

        # Hessian of the QP cost, summed from the four squared-residual terms.
        # ``H_raw`` is symmetrised into ``H`` and later doubled into
        # ``H_scaled`` for DAQP's ½ convention (see the note below).
        H_raw = (
            w_c * (Y_T_u.T @ Y_T_u)
            + w_e * np.eye(N)
            + w_du * (D_diff.T @ D_diff)
            + w_i * (I_T_u.T @ I_T_u)
        )
        H = 0.5 * (H_raw + H_raw.T)
        g = (
            w_c * (Y_T_u.T @ track_const)
            - w_e * u_ss * np.ones(N)
            - w_du * (D_diff.T @ b_du)
            + w_i * (I_T_u.T @ I_const)
        )

        delta_u_max = self.params.delta_u_max
        u_min = self.params.u_min
        u_max = self.params.u_max
        A_box = np.eye(N)
        A_con_dense = np.vstack([A_box, D_diff])
        lb = np.concatenate([np.full(N, u_min), -delta_u_max + b_du])
        ub = np.concatenate([np.full(N, u_max), +delta_u_max + b_du])

        # DAQP minimises ½·xᵀ·H·x + fᵀ·x. The cost is a sum of squared
        # residuals J(u) = w_c‖Y·u + c‖² + w_e‖u − u_ss‖² + w_du‖D·u − b‖² +
        # w_i‖I·u + e‖²; ``H_raw`` is its Hessian/2 and ``g`` its gradient/2, so
        # both are scaled by 2 to match DAQP's ½ convention (scaling H alone
        # would halve the linear term and bias the solution).
        H_scaled = 2.0 * H
        g_scaled = 2.0 * g
        if DAQP_AVAILABLE and _daqp is not None:
            try:
                bsense = np.zeros(A_con_dense.shape[0], dtype=np.int32)
                x, _fval, exitflag, _info = _daqp.solve(
                    H_scaled, g_scaled, A_con_dense, ub, lb, bsense
                )
                if exitflag == 1:
                    return _bounded_command(float(x[0]), u_last, u_min, u_max)
                self._report_daqp_failure(f"exit flag {exitflag}")
            except (ArithmeticError, RuntimeError, ValueError) as err:
                # DAQP is an optional accelerator. A numerical failure must
                # not disable heating when the portable solver can continue.
                self._report_daqp_failure(repr(err))

        x = self._solve_portable(
            H_scaled,
            g_scaled,
            _SolverBounds(
                u_last=u_last, u_min=u_min, u_max=u_max, delta_u_max=delta_u_max
            ),
        )
        # ``x[0]`` is a numpy scalar; convert once to a plain float so the
        # caller doesn't propagate numpy types into JSON-bound state.
        return _bounded_command(float(x[0]), u_last, u_min, u_max)

    def _report_daqp_failure(self, reason: str) -> None:
        """Log that daqp gave no plan and the NumPy solver computes it."""
        level = logging.DEBUG if self._daqp_failure_reported else logging.WARNING
        self._daqp_failure_reported = True
        _LOGGER.log(
            level,
            "MPC v2 daqp solve failed (%s); the NumPy solver computed the plan",
            reason,
        )

    def _solve_portable(
        self, hessian: FloatArray, gradient: FloatArray, bounds: _SolverBounds
    ) -> FloatArray:
        """Solve the small convex QP with a NumPy interior-point method.

        Minimises ``½·xᵀ·H·x + gᵀ·x`` under the valve box and the rate limits,
        written as one-sided rows ``C·x ≤ d`` in which the first step's box
        and rate limit are merged into one interval. A Mehrotra
        predictor-corrector iteration converges without the combinatorial
        stalls an active-set search meets on ties; the constraints it ends
        on then define an equality problem whose exact solution replaces the
        iterate when it is feasible and its multipliers have the right sign.
        A plan the best flat plan beats is replaced by it. Where daqp
        returns a feasible optimum in the tests, the first command agrees
        with it to 1e-4 percentage points; the tests require at least 700
        such comparisons across the plants and weight settings they draw.

        When the plan space collapses (no rate or box width), or the
        iteration fails or ends infeasible, the best flat plan is returned
        instead, which is always feasible. A non-finite objective holds the
        last command. Either event is logged at WARNING the first time an
        optimiser meets it and at DEBUG after that.
        """
        n = self.N
        u_last = bounds.u_last
        u_min = bounds.u_min
        u_max = bounds.u_max
        delta_u_max = bounds.delta_u_max

        # The interval of the first command is empty only for an invalid
        # configuration; the safely clamped command is returned then.
        first_lo = max(u_min, u_last - delta_u_max)
        first_hi = min(u_max, u_last + delta_u_max)
        if first_lo > first_hi:
            return np.full(n, max(u_min, min(u_max, u_last)))
        # A non-finite objective has no optimum; the last command holds.
        if not (np.all(np.isfinite(hessian)) and np.all(np.isfinite(gradient))):
            level = logging.DEBUG if self._non_finite_reported else logging.WARNING
            self._non_finite_reported = True
            _LOGGER.log(
                level, "MPC v2 plan objective is not finite; holding the last command"
            )
            return np.full(n, float(np.clip(u_last, first_lo, first_hi)))
        # Every flat plan inside the first interval is feasible; the best of
        # them is the fallback.
        ones = np.ones(n)
        curvature = float(ones @ hessian @ ones)
        level = -float(ones @ gradient) / curvature if curvature > 0.0 else u_last
        flat = np.full(n, float(np.clip(level, first_lo, first_hi)))
        if delta_u_max <= 0.0 or u_max <= u_min:
            return flat

        rise = (np.eye(n) - np.eye(n, k=-1))[1:]
        rows = np.vstack([np.eye(n), -np.eye(n), rise, -rise])
        upper = np.full(n, u_max)
        upper[0] = first_hi
        lower = np.full(n, u_min)
        lower[0] = first_lo
        limits = np.concatenate(
            [upper, -lower, np.full(n - 1, delta_u_max), np.full(n - 1, delta_u_max)]
        )
        plan = self._interior_point(hessian, gradient, rows, limits, flat)
        if plan is None or float(np.max(rows @ plan - limits)) > _FEASIBILITY_TOL:
            level = logging.DEBUG if self._flat_fallback_reported else logging.WARNING
            self._flat_fallback_reported = True
            _LOGGER.log(
                level, "MPC v2 NumPy solver found no feasible optimum; planning flat"
            )
            return flat

        # An iteration that ends short of the optimum can leave a plan the
        # best flat plan beats; the better of the two is planned.
        def objective(x: FloatArray) -> float:
            return float(0.5 * x @ hessian @ x + gradient @ x)

        return flat if objective(flat) < objective(plan) else plan

    @staticmethod
    def _interior_point(
        hessian: FloatArray,
        gradient: FloatArray,
        rows: FloatArray,
        limits: FloatArray,
        start: FloatArray,
    ) -> FloatArray | None:
        """Return the optimum of ``½xᵀHx + gᵀx`` s.t. ``rows·x ≤ limits``.

        An iteration that stops short of the optimum returns its last
        iterate, and a zero Hessian returns None. The objective is scaled to
        a unit Hessian entry first, so the tolerances mean the same for every
        weight setting.
        """
        count = rows.shape[0]
        scale = float(np.max(np.abs(hessian)))
        if scale <= 0.0:
            return None
        h = hessian / scale
        g = gradient / scale
        # The multipliers grow with the gradient, so their residuals are
        # judged relative to it.
        dual_scale = 1.0 + float(np.max(np.abs(g)))
        x = start.copy()
        slack = np.maximum(limits - rows @ x, 1.0)
        dual = np.ones(count)
        for _ in range(_INTERIOR_POINT_ITERATIONS):
            dual_residual = h @ x + g + rows.T @ dual
            primal_residual = rows @ x + slack - limits
            gap = float(slack @ dual) / count
            residual = max(
                float(np.max(np.abs(dual_residual))) / dual_scale,
                float(np.max(np.abs(primal_residual))),
                gap,
            )
            if residual < _CONVERGENCE_TOL:
                exact = _polish(h, g, rows, limits, slack, dual)
                return x if exact is None else exact
            weight = dual / slack
            try:
                factor = np.linalg.cholesky(h + rows.T @ (weight[:, None] * rows))
            except np.linalg.LinAlgError:
                # Near the optimum the weights span many orders of magnitude
                # and the Newton matrix can lose definiteness to round-off;
                # the exact solve finishes the iterate when it can.
                exact = _polish(h, g, rows, limits, slack, dual)
                return x if exact is None else exact

            residuals = (dual_residual, primal_residual)
            dx, d_slack, d_dual = _newton_direction(
                factor, rows, slack, dual, residuals, slack * dual
            )
            affine = min(
                _step_to_boundary(slack, d_slack), _step_to_boundary(dual, d_dual)
            )
            affine_gap = float((slack + affine * d_slack) @ (dual + affine * d_dual))
            sigma = (affine_gap / count / gap) ** 3 if gap > 0.0 else 0.0
            dx, d_slack, d_dual = _newton_direction(
                factor,
                rows,
                slack,
                dual,
                residuals,
                slack * dual + d_slack * d_dual - sigma * gap,
            )
            step = 0.995 * min(
                _step_to_boundary(slack, d_slack), _step_to_boundary(dual, d_dual)
            )
            x = x + step * dx
            slack = slack + step * d_slack
            dual = dual + step * d_dual
        exact = _polish(h, g, rows, limits, slack, dual)
        return x if exact is None else exact

    def _steady_input_for(
        self, T_sp: float, T_outdoor: float, D_hat_K_per_min: float = 0.0
    ) -> float:
        u_ss = self.plant.steady_input(T_sp, T_outdoor, D_hat_K_per_min)
        return max(0.0, min(1.0, u_ss))
