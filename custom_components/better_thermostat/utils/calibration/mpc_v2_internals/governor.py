"""Scalar Reference Governor — shapes large setpoint jumps.

Implements the static feasibility variant from Garone, Di Cairano,
Kolmanovsky (Automatica 75, 2017). Each step computes

    v_k = v_{k-1} + κ · (T_sp − v_{k-1})

with ``κ ∈ [0,1]`` the largest value for which the steady-state input
implied by ``v_k`` stays inside ``[u_min + δ, u_max − δ]``. When the user
setpoint is already feasible, the governor is transparent (``κ = 1``).

Only the static feasibility variant is implemented. The dynamic (rollout)
variant is out of scope: it needs per-plant tuning of horizon and overshoot
tolerance and is over-conservative on the default profile.
"""

from __future__ import annotations

from dataclasses import dataclass

from .plant import PlantModelRC2


@dataclass
class GovernorParams:
    """Tunables for the scalar reference governor."""

    enabled: bool = True
    bisection_iters: int = 8
    safety_margin: float = 0.02
    u_min: float = 0.0
    u_max: float = 1.0


class ScalarReferenceGovernor:
    """Shapes setpoint jumps so the implied steady-state input stays feasible."""

    def __init__(self, plant: PlantModelRC2, params: GovernorParams) -> None:
        """Bind the plant and tunables and clear the governed reference.

        Parameters
        ----------
        plant : PlantModelRC2
            RC2 plant used to compute the steady-state input feasibility.
        params : GovernorParams
            Tunables for the governor (enable flag, bisection iterations,
            safety margin, input bounds).
        """
        self.plant = plant
        self.params = params
        self._v_C: float | None = None

    def reset(self) -> None:
        """Clear the governed reference so the next update re-seeds it."""
        self._v_C = None

    def state(self) -> float | None:
        """Return the current governed reference, or ``None`` if unset."""
        return self._v_C

    def restore(self, v_C: float | None) -> None:
        """Restore a previously persisted governed reference."""
        self._v_C = v_C

    def _u_steady_for(self, v_C: float, T_outdoor_C: float) -> float:
        return self.plant.steady_input(v_C, T_outdoor_C)

    def _is_feasible(self, v_C: float, T_outdoor_C: float) -> bool:
        u_ss = self._u_steady_for(v_C, T_outdoor_C)
        delta = self.params.safety_margin
        return self.params.u_min + delta <= u_ss <= self.params.u_max - delta

    def update(self, T_sp: float, T_outdoor_C: float, T_room_now: float) -> float:
        """Return the governed reference for this step.

        Passes ``T_sp`` through unchanged when disabled or already feasible.
        Otherwise bisects ``κ ∈ [0,1]`` to find the largest feasible step from
        the previous reference toward ``T_sp`` and advances by that fraction.
        """
        if not self.params.enabled:
            return T_sp
        if self._v_C is None:
            self._v_C = T_room_now
        if self._is_feasible(T_sp, T_outdoor_C):
            self._v_C = T_sp
            return self._v_C
        v_prev = self._v_C
        lo, hi = 0.0, 1.0
        for _ in range(self.params.bisection_iters):
            mid = 0.5 * (lo + hi)
            v_trial = v_prev + mid * (T_sp - v_prev)
            if self._is_feasible(v_trial, T_outdoor_C):
                lo = mid
            else:
                hi = mid
        self._v_C = v_prev + lo * (T_sp - v_prev)
        return self._v_C
