"""Finite-horizon QP MPC — DAQP-backed receding-horizon optimiser.

Solves over ``u = [u_0, …, u_{N-1}]``:

    J = Σ_k  w_c·(T_room_k − T_sp)²
            + w_e·(u_k − u_ss)²
            + w_du·(u_k − u_{k-1})²
            + w_i·(I_k − 0)²

where ``I_k`` is the predicted integrated tracking error. Hard constraints
``u ∈ [u_min, u_max]`` and ``|Δu| ≤ Δu_max`` are encoded directly.

DAQP (dense active-set) is preferred over OSQP for two reasons: the wheel
is ~30× smaller (~100 KB vs ~3 MB) — important for HA installs that
bundle the integration over HACS — and there's no setup phase to amortise
when re-solving every step with a freshly linearised plant model.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any

import numpy as np

from ._types import FloatArray
from .plant import PlantModelRC2


def _try_import_daqp() -> tuple[Any | None, str | None]:
    """Return ``(module, None)`` on success or ``(None, error_message)``.

    ``daqp`` is an optional HA runtime requirement (manifest.json) that may be
    absent in the dev/CI env and ships no type stubs; importing it by name via
    ``importlib`` keeps the static checkers from resolving (and flagging) it.
    The wrapping function also avoids ``reportConstantRedefinition`` on the
    module-level result flags, which are written once at import.
    """
    try:
        return importlib.import_module("daqp"), None
    except ImportError as err:  # pragma: no cover — exercised only on broken installs
        return None, str(err)


_daqp, _DAQP_IMPORT_ERROR = _try_import_daqp()
DAQP_AVAILABLE = _daqp is not None


def require_daqp() -> Any:
    """Return the imported daqp module, raising a descriptive ImportError if absent.

    DAQP ships as a small native wheel that occasionally fails to install on
    exotic platforms. We check at controller construction so the user gets a
    clear message in the HA log instead of a stack trace from inside the
    first QP solve.
    """
    if not DAQP_AVAILABLE or _daqp is None:
        raise ImportError(
            "MPC v2 requires the 'daqp' QP solver but the import failed: "
            f"{_DAQP_IMPORT_ERROR}. Pick a different calibration_mode or "
            "install the wheel manually in the HA Python environment."
        )
    return _daqp


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
    # Plant-aware step-size scaling. With ``adaptive_step_s=True`` the
    # ``step_s`` field above is recomputed at controller construction as
    # ``clamp(min, max, tau_room · per_tau)`` so fast envelopes get a finer
    # prediction grid and slow envelopes reuse the default ~300 s.
    adaptive_step_s: bool = True
    adaptive_step_s_per_tau: float = 0.6
    adaptive_step_s_min: float = 90.0
    adaptive_step_s_max: float = 300.0


class QpOptimiser:
    """DAQP-backed receding-horizon optimiser returning the next valve fraction."""

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

    def reset_integral(self) -> None:
        """Clear the accumulated integral tracking error."""
        self.e_integral_K_min = 0.0

    def update_integral(
        self, T_room: float, T_sp: float, u_applied: float, dt_s: float
    ) -> None:
        """Accumulate the tracking error with anti-windup and clipping.

        Skips accumulation when the applied input is saturated and the error
        sign would only grow the integral further, then clips the running total
        to ``±integral_clip_K_min``.
        """
        err = T_room - T_sp
        band = self.params.saturation_band
        at_upper = u_applied >= self.params.u_max - band
        at_lower = u_applied <= self.params.u_min + band
        if (at_upper and err < 0) or (at_lower and err > 0):
            return
        self.e_integral_K_min += (dt_s / 60.0) * err
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

        Builds the condensed prediction matrices from the linearised plant,
        assembles the Hessian and gradient, and calls DAQP under box and
        rate-limit constraints. On solver failure returns the clamped previous
        input ``u_last`` as a safe fallback.
        """
        daqp = require_daqp()

        n = self.plant.state_dim
        N = self.N

        x_target = self._steady_state_for(T_sp, T_outdoor_C)
        u_ss = self._steady_input_for(T_sp, T_outdoor_C, D_hat_K_per_min)
        A, B, d_vec = self.plant.linearised_AB(T_outdoor_C, float(x_target[1]))

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
        bsense = np.zeros(A_con_dense.shape[0], dtype=np.int32)
        x, _fval, exitflag, _info = daqp.solve(
            H_scaled, g_scaled, A_con_dense, ub, lb, bsense
        )
        if exitflag != 1:
            return max(u_min, min(u_max, u_last))
        # ``x[0]`` is a numpy scalar; convert once to a plain float so the
        # caller doesn't propagate numpy types into JSON-bound state.
        return max(u_min, min(u_max, float(x[0])))

    def _steady_state_for(self, T_sp: float, T_outdoor_C: float) -> FloatArray:
        T_rad_ss = self.plant.steady_radiator_temp(T_sp, T_outdoor_C)
        return np.array([T_sp, T_rad_ss])

    def _steady_input_for(
        self, T_sp: float, T_outdoor_C: float, D_hat_K_per_min: float = 0.0
    ) -> float:
        u_ss = self.plant.steady_input(T_sp, T_outdoor_C, D_hat_K_per_min)
        return max(0.0, min(1.0, u_ss))
