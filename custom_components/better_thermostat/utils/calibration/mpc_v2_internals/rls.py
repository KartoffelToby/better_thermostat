# pyright: reportConstantRedefinition=false
# Linear-algebra convention: ``P`` is the RLS covariance matrix (mutable).
"""RLS identifier for the RC2 room-balance parameters.

Estimates ``θ = [a, b]`` from ``dT_room/dt ≈ a·(T_rad − T_room) − b·(T_room − T_out)``
where ``a = coupling/τ_room`` and ``b = 1/τ_room``. Both physically positive;
results are projected onto box bounds after each update and EMA-blended
into the active plant params so the QP gain doesn't jump per sample.

Updates are excitation-gated (skipped when ‖φ‖ is small or P-conditioning
explodes), so a constant-setpoint trim run won't drift the estimate on
noise alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from ._types import FloatArray
from .plant import PlantModelRC2


@dataclass
class RlsParams:
    """Tunables for the RLS identifier (forgetting, bounds, excitation gates)."""

    lam: float = 0.999
    excitation_threshold: float = 1e4
    tau_room_bounds: tuple[float, float] = (60.0, 2000.0)
    coupling_bounds: tuple[float, float] = (0.2, 2.0)
    plant_update_tau_s: float = 1800.0
    # Minimum regressor norm ``‖φ‖`` to accept an update. Below this the
    # system is effectively unexcited (constant setpoint, no disturbances)
    # and folding in the sample would only let noise drift the estimate.
    excitation_norm_min: float = 0.05


@dataclass
class RlsStateSnapshot:
    """JSON-serialisable snapshot of the RLS estimator state.

    Lists (not numpy arrays) so it round-trips through the HA Store unchanged;
    :meth:`from_mapping` rebuilds it defensively from a persisted dict.
    """

    theta: list[float]
    P: list[list[float]]
    prev_T_room: float | None
    prev_t_s: float | None
    update_count: int
    skip_count: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RlsStateSnapshot:
        """Build a snapshot from a raw persisted mapping, tolerating gaps."""
        prev_room = raw.get("prev_T_room")
        prev_t = raw.get("prev_t_s")
        return cls(
            theta=[float(x) for x in raw.get("theta", [])],
            P=[[float(x) for x in row] for row in raw.get("P", [])],
            prev_T_room=None if prev_room is None else float(prev_room),
            prev_t_s=None if prev_t is None else float(prev_t),
            update_count=int(raw.get("update_count", 0)),
            skip_count=int(raw.get("skip_count", 0)),
        )


class RLSIdentifier:
    """Recursive least-squares estimator for the RC2 room-balance parameters."""

    # Math-convention attribute names; declared here so strict type checkers
    # don't read the uppercase as ``Final`` and flag mutation.
    P: FloatArray
    theta: FloatArray
    _prev_T_room: float | None
    _prev_t_s: float | None

    def __init__(self, plant: PlantModelRC2, params: RlsParams) -> None:
        """Seed the estimate and covariance from the plant's current params.

        Initialises ``theta = [a, b]`` from the plant's coupling and room time
        constant, sets the RLS covariance ``P``, and clears the measurement
        history and update/skip counters.

        Parameters
        ----------
        plant : PlantModelRC2
            RC2 plant whose room-balance parameters are estimated and updated.
        params : RlsParams
            Forgetting factor, parameter bounds, and excitation-gate tunables.
        """
        self.plant = plant
        self.params = params
        self._seed()

    def _seed(self) -> None:
        """Seed estimate, covariance, history, and counters from the plant prior."""
        p = self.plant.params
        a0 = p.coupling_rad_room / max(p.tau_room_min, 1.0)
        b0 = 1.0 / max(p.tau_room_min, 1.0)
        self.theta = np.array([a0, b0])
        # Initial RLS covariance for ``θ = [a, b]``. The ``b = 1/τ_room``
        # channel starts an order of magnitude tighter (1e-6 vs 1e-4) because
        # the room time constant prior is far more trustworthy than the
        # coupling prior, so we let RLS move ``a`` more readily than ``b``.
        self.P = np.diag([1e-4, 1e-6])
        self._prev_T_room = None
        self._prev_t_s = None
        self.update_count = 0
        self.skip_count = 0

    def reset(self) -> None:
        """Reset the estimate, covariance, history, and counters to defaults."""
        self._seed()

    def export_state(self) -> RlsStateSnapshot:
        """Return a snapshot of the estimator state (estimate, covariance, …)."""
        return RlsStateSnapshot(
            theta=[float(x) for x in self.theta],
            P=[[float(x) for x in row] for row in self.P],
            prev_T_room=self._prev_T_room,
            prev_t_s=self._prev_t_s,
            update_count=self.update_count,
            skip_count=self.skip_count,
        )

    def restore_state(self, snap: RlsStateSnapshot) -> None:
        """Rehydrate the estimator from an :meth:`export_state` snapshot.

        Empty estimate/covariance (a partial snapshot) leaves the seeded
        defaults in place rather than zeroing the estimator.
        """
        if snap.theta:
            self.theta = np.asarray(snap.theta, dtype=float)
        if snap.P:
            self.P = np.asarray(snap.P, dtype=float)
        self._prev_T_room = snap.prev_T_room
        self._prev_t_s = snap.prev_t_s
        self.update_count = snap.update_count
        self.skip_count = snap.skip_count

    def update(
        self, t_s: float, T_room_C: float, T_rad_C: float, T_outdoor_C: float
    ) -> None:
        """Run one excitation-gated RLS step from the latest measurements.

        Forms the regressor from the room-balance equation and updates the
        estimate, projecting onto the box bounds and folding the result into
        the plant params. Skips the update (and seeds history on the first
        call) when ``dt`` is non-positive, excitation is too low, or the
        covariance conditioning explodes.
        """
        if self._prev_T_room is None or self._prev_t_s is None:
            self._prev_T_room = T_room_C
            self._prev_t_s = t_s
            return
        dt_s = t_s - self._prev_t_s
        if dt_s <= 0.0:
            return
        dt_min = dt_s / 60.0
        y = (T_room_C - self._prev_T_room) / dt_min
        phi = np.array([T_rad_C - T_room_C, -(T_room_C - T_outdoor_C)])
        self._prev_T_room = T_room_C
        self._prev_t_s = t_s

        if np.linalg.norm(phi) < self.params.excitation_norm_min:
            self.skip_count += 1
            return

        lam = self.params.lam
        Pphi = self.P @ phi
        denom = lam + phi @ Pphi
        if denom <= 1e-12:
            self.skip_count += 1
            return
        g = Pphi / denom
        innovation = y - phi @ self.theta
        new_theta = self._project(self.theta + g * innovation)
        new_P = (self.P - np.outer(g, Pphi)) / lam
        if np.linalg.cond(new_P) > self.params.excitation_threshold:
            self.skip_count += 1
            return
        self.theta = new_theta
        self.P = new_P
        self.update_count += 1
        self._fold_into_plant(dt_s)

    def _project(self, theta: FloatArray) -> FloatArray:
        a, b = float(theta[0]), float(theta[1])
        tau_lo, tau_hi = self.params.tau_room_bounds
        c_lo, c_hi = self.params.coupling_bounds
        tau = 1.0 / max(b, 1e-6)
        coupling = a / max(b, 1e-6)
        tau = max(tau_lo, min(tau_hi, tau))
        coupling = max(c_lo, min(c_hi, coupling))
        b_new = 1.0 / tau
        a_new = coupling * b_new
        return np.array([a_new, b_new])

    def _fold_into_plant(self, dt_s: float) -> None:
        """EMA-blend the latest RLS estimate into the active plant params.

        ``alpha`` is derived from the *actual* sample interval ``dt_s`` and
        the configured plant-update time constant — so callers that step
        every 30 s and callers that step every 5 min both converge over
        the same wall-clock duration instead of the slower-cadence caller
        adapting that-many-times slower.
        """
        a, b = float(self.theta[0]), float(self.theta[1])
        target_tau = 1.0 / max(b, 1e-6)
        target_coupling = a / max(b, 1e-6)
        tau_filter = max(self.params.plant_update_tau_s, dt_s)
        alpha = max(0.0, min(1.0, dt_s / tau_filter))
        current_tau = self.plant.params.tau_room_min
        current_coupling = self.plant.params.coupling_rad_room
        self.plant.params.tau_room_min = (
            1.0 - alpha
        ) * current_tau + alpha * target_tau
        self.plant.params.coupling_rad_room = (
            1.0 - alpha
        ) * current_coupling + alpha * target_coupling
