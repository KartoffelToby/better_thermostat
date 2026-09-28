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
architecture, so a small active-set solver using only NumPy finds the same
optimum under the same hard valve constraints everywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
from typing import Any

import numpy as np

from ._types import FloatArray
from .plant import PlantModelRC2

_LOGGER = logging.getLogger(__name__)


def _try_import_daqp() -> Any | None:
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
        # A daqp failure is reported once per optimiser at WARNING, later
        # ones at DEBUG, so a solver that keeps failing does not flood the log.
        self._daqp_failure_reported = False
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
        T_outdoor_C: float,
        u_last: float,
        D_hat_K_per_min: float = 0.0,
    ) -> float:
        """Solve the horizon QP and return the first valve command ``u_0``.

        Builds the condensed prediction matrices from the linearised plant and
        assembles the Hessian and gradient. DAQP solves the small dense QP when
        available; otherwise a NumPy active-set solver finds the same optimum
        under the same box bounds and rate limits.
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
            self.plant.steady_radiator_temp(T_sp, T_outdoor_C, D_hat_K_per_min),
            self.plant.hottest_radiator_temp(T_sp),
        )
        u_ss = self._steady_input_for(T_sp, T_outdoor_C, D_hat_K_per_min)
        A, B, d_vec = self.plant.linearised_AB(T_outdoor_C, radiator_operating_point)
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
                    return max(u_min, min(u_max, float(x[0])))
                self._report_daqp_failure(f"exit flag {exitflag}")
            except (ArithmeticError, RuntimeError, ValueError) as err:
                # DAQP is an optional accelerator. A numerical failure must
                # not disable heating when the portable solver can continue.
                self._report_daqp_failure(repr(err))

        x = self._solve_active_set(
            H_scaled,
            g_scaled,
            _SolverBounds(
                u_last=u_last, u_min=u_min, u_max=u_max, delta_u_max=delta_u_max
            ),
        )
        # ``x[0]`` is a numpy scalar; convert once to a plain float so the
        # caller doesn't propagate numpy types into JSON-bound state.
        return max(u_min, min(u_max, float(x[0])))

    def _report_daqp_failure(self, reason: str) -> None:
        """Log that daqp gave no plan and the NumPy solver computes it."""
        level = logging.DEBUG if self._daqp_failure_reported else logging.WARNING
        self._daqp_failure_reported = True
        _LOGGER.log(
            level,
            "MPC v2 daqp solve failed (%s); the NumPy solver computed the plan",
            reason,
        )

    def _solve_active_set(
        self, hessian: FloatArray, gradient: FloatArray, bounds: _SolverBounds
    ) -> FloatArray:
        """Solve the small convex QP exactly with a primal active-set method.

        Minimises ``½·xᵀ·H·x + gᵀ·x`` under the valve box and the rate limits,
        written as one-sided rows ``C·x ≤ d``. The iterate stays feasible from
        a flat start; each step solves the equality-constrained problem on the
        working set, stops at the first blocking constraint and drops the one
        with the most negative multiplier once the step vanishes. ``H`` is
        positive definite, so this ends in the unique optimum daqp finds.
        """
        n = self.N
        if n <= 0:
            return np.empty(0)
        u_last = bounds.u_last
        u_min = bounds.u_min
        u_max = bounds.u_max
        delta_u_max = bounds.delta_u_max

        # A flat trajectory at the command closest to ``u_last`` is feasible.
        # The interval is empty only for an invalid configuration; the safely
        # clamped command is returned then.
        first_lo = max(u_min, u_last - delta_u_max)
        first_hi = min(u_max, u_last + delta_u_max)
        if first_lo > first_hi:
            return np.full(n, max(u_min, min(u_max, u_last)))
        x = np.full(n, float(np.clip(u_last, first_lo, first_hi)))
        # A non-finite objective has no optimum; the flat trajectory holds.
        if not (np.all(np.isfinite(hessian)) and np.all(np.isfinite(gradient))):
            return x

        diff = np.eye(n) - np.eye(n, k=-1)
        rate_offset = np.zeros(n)
        rate_offset[0] = u_last
        rows = np.vstack([np.eye(n), -np.eye(n), diff, -diff])
        limits = np.concatenate(
            [
                np.full(n, u_max),
                np.full(n, -u_min),
                delta_u_max + rate_offset,
                delta_u_max - rate_offset,
            ]
        )
        # A step or multiplier this small is round-off of the KKT solve; the
        # valve fraction it stands for is far below one percent.
        tol = 1e-9
        working: list[int] = []
        for _ in range(10 * rows.shape[0]):
            active = rows[working]
            kkt = np.block(
                [[hessian, active.T], [active, np.zeros((len(working), len(working)))]]
            )
            rhs = np.concatenate([-(hessian @ x + gradient), np.zeros(len(working))])
            # Only a constraint the step moves towards joins the working set,
            # so its rows stay independent and the KKT matrix regular. A
            # singular one can only come from a degenerate Hessian; the
            # feasible iterate reached so far is kept then.
            try:
                solution = np.linalg.solve(kkt, rhs)
            except np.linalg.LinAlgError:
                break
            step = solution[:n]
            if float(np.max(np.abs(step))) <= tol:
                multipliers = solution[n:]
                if not working or float(np.min(multipliers)) >= -tol:
                    break
                working.pop(int(np.argmin(multipliers)))
                continue
            growth = rows @ step
            slack = limits - rows @ x
            length = 1.0
            blocking = -1
            for idx in range(rows.shape[0]):
                if idx in working or growth[idx] <= 1e-12:
                    continue
                ratio = max(0.0, float(slack[idx])) / float(growth[idx])
                if ratio < length:
                    length = ratio
                    blocking = idx
            x = x + length * step
            if blocking >= 0:
                working.append(blocking)
        return x

    def _steady_input_for(
        self, T_sp: float, T_outdoor_C: float, D_hat_K_per_min: float = 0.0
    ) -> float:
        u_ss = self.plant.steady_input(T_sp, T_outdoor_C, D_hat_K_per_min)
        return max(0.0, min(1.0, u_ss))
