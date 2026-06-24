"""MPC v2 controller stack and its persistence snapshot."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
from typing import Any

import numpy as np

from ..mpc_v2_internals.dob import DisturbanceObserver
from ..mpc_v2_internals.governor import ScalarReferenceGovernor
from ..mpc_v2_internals.kalman import KalmanObserver
from ..mpc_v2_internals.plant import PlantModelRC2
from ..mpc_v2_internals.qp_optimiser import QpOptimiser, require_daqp
from ..mpc_v2_internals.rls import RLSIdentifier, RlsStateSnapshot
from ..mpc_v2_internals.smith import SmithPredictor
from .io import MpcV2Diagnostics
from .params import MpcV2Params

_LOGGER = logging.getLogger(__name__)

# Snapshot format version. Bump when adding/renaming persisted fields so
# restore_snapshot can refuse payloads from a future Better Thermostat
# release. Pre-versioning snapshots are treated as version 0.
SNAPSHOT_VERSION = 1


@dataclass
class ControllerSnapshot:
    """Typed, JSON-round-trippable snapshot of the full controller state.

    Field names are the persisted JSON keys; ``asdict`` produces the stored
    mapping and :meth:`from_mapping` rebuilds it defensively on load. Arrays are
    plain lists so the HA Store round-trips them unchanged.
    """

    v: int
    x_hat: list[float]
    kalman_P: list[list[float]]
    D_hat_K_per_min: float
    last_u: float
    e_integral_K_min: float
    u_history: list[float]
    plant_params: dict[str, float]
    rg_v_C: float | None
    last_t_s: float
    next_mpc_t_s: float
    rls: RlsStateSnapshot | None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ControllerSnapshot | None:
        """Parse a persisted mapping; return ``None`` for a future version.

        This is the single place raw (untyped) persisted data is validated and
        coerced; missing keys fall back to the controller's construction
        defaults, so a partial snapshot degrades gracefully.
        """
        version = int(raw.get("v", 0))
        if version > SNAPSHOT_VERSION:
            _LOGGER.warning(
                "MPC v2 snapshot version %d > supported %d; ignoring",
                version,
                SNAPSHOT_VERSION,
            )
            return None
        rls_raw = raw.get("rls")
        plant_raw: Mapping[str, Any] = raw.get("plant_params") or {}
        return cls(
            v=version,
            x_hat=[float(x) for x in raw.get("x_hat", [])],
            kalman_P=[[float(x) for x in row] for row in raw.get("kalman_P", [])],
            D_hat_K_per_min=float(raw.get("D_hat_K_per_min", 0.0)),
            last_u=float(raw.get("last_u", 0.0)),
            e_integral_K_min=float(raw.get("e_integral_K_min", 0.0)),
            u_history=[float(x) for x in raw.get("u_history", [])],
            plant_params={k: float(v) for k, v in plant_raw.items()},
            rg_v_C=None if raw.get("rg_v_C") is None else float(raw["rg_v_C"]),
            last_t_s=float(raw.get("last_t_s", 0.0)),
            next_mpc_t_s=float(raw.get("next_mpc_t_s", -1.0)),
            rls=(
                RlsStateSnapshot.from_mapping(rls_raw)
                if isinstance(rls_raw, Mapping)
                else None
            ),
        )


class MpcV2Controller:
    """Glue: plant + Kalman + Smith + DOB + RLS + governor + QP.

    Lifetime is one room. The controller mutates its plant params via RLS
    over time; rehydration from persistent state goes through
    :meth:`export_snapshot` / :meth:`restore_snapshot`.
    """

    def __init__(self, mpc_params: MpcV2Params) -> None:
        """Build the controller stack (plant, observers, optimiser, governor).

        Parameters
        ----------
        mpc_params : MpcV2Params
            Plant prior plus observer/optimiser/governor/RLS tunables. The
            mutated sub-params (``plant``, ``qp``) are copied so online RLS
            updates never mutate the caller's object.
        """
        # Fail fast when the daqp wheel is missing so the HA log carries
        # a clear message instead of crashing on the first QP solve.
        require_daqp()
        # Copy the two sub-params this controller mutates in place — ``plant``
        # (folded by RLS) and ``qp`` (``step_s`` rewritten below) — so the
        # caller's object is never aliased. Both are flat dataclasses, so a
        # shallow ``replace`` fully isolates them; the read-only kalman/dob/rls/
        # governor params are shared by reference. Aliasing the caller's plant
        # would drift ``_plant_signature_of(params)`` from the build-time
        # signature and falsely trip the preset-change rebuild guard.
        mpc_params = replace(
            mpc_params, plant=replace(mpc_params.plant), qp=replace(mpc_params.qp)
        )
        self.params = mpc_params
        # Plant-aware MPC step: faster envelopes ⇒ finer grid. One-shot at
        # construction to avoid rebuilding the QP workspace on every RLS
        # update.
        if mpc_params.qp.adaptive_step_s:
            tau_eff = max(mpc_params.plant.tau_room_min, 1.0)
            target = tau_eff * mpc_params.qp.adaptive_step_s_per_tau
            mpc_params.qp.step_s = max(
                mpc_params.qp.adaptive_step_s_min,
                min(mpc_params.qp.adaptive_step_s_max, target),
            )
        self.plant_fine = PlantModelRC2(mpc_params.plant, dt_s=mpc_params.plant_step_s)
        self.plant_coarse = PlantModelRC2(mpc_params.plant, dt_s=mpc_params.qp.step_s)
        self.kalman = KalmanObserver(self.plant_fine, mpc_params.kalman)
        self.smith = SmithPredictor(self.plant_fine)
        self.dob = DisturbanceObserver(mpc_params.dob)
        self.optimiser = QpOptimiser(self.plant_coarse, mpc_params.qp)
        self.governor = ScalarReferenceGovernor(self.plant_coarse, mpc_params.governor)
        self.rls = (
            RLSIdentifier(self.plant_fine, mpc_params.rls)
            if mpc_params.enable_rls
            else None
        )

        self._u_history: deque[float] = deque(maxlen=64)
        self._last_u: float = 0.0
        self._last_t_s: float = 0.0
        self._next_mpc_t_s: float = -1.0
        self._initialised: bool = False

    def step(
        self,
        t_s: float,
        T_room_C: float,
        T_target_C: float,
        T_outdoor_C: float,
        T_rad_C: float | None = None,
    ) -> tuple[float, MpcV2Diagnostics]:
        """Run one control cycle. Returns (valve_fraction, diagnostics).

        Parameters
        ----------
        t_s : float
            Wall-clock timestamp of this cycle in seconds; drives the dt used
            by the disturbance observer and RLS, and the MPC re-plan cadence.
        T_room_C : float
            Measured room temperature — the sole Kalman measurement.
        T_target_C : float
            Setpoint handed to the reference governor and QP.
        T_outdoor_C : float
            Outdoor temperature for the loss term and feed-forward.
        T_rad_C : float | None, optional
            Measured radiator temperature. Used only to seed the initial
            Kalman estimate on the very first cycle (falling back to
            ``T_room_C`` when ``None``); ignored on every subsequent cycle.
        """
        if not self._initialised:
            T_rad_init = T_rad_C if T_rad_C is not None else T_room_C
            self.kalman.initialise(np.array([T_room_C, T_rad_init]))
            self._next_mpc_t_s = t_s
            self._initialised = True

        dt_s = t_s - self._last_t_s if self._last_t_s > 0 else self.params.plant_step_s
        self._last_t_s = t_s

        sp_for_opt = self.governor.update(
            T_sp=T_target_C, T_outdoor_C=T_outdoor_C, T_room_now=T_room_C
        )

        innovation = self.kalman.innovation(T_room_C, self._last_u, T_outdoor_C)
        x_hat = self.kalman.update(T_room_C, self._last_u, T_outdoor_C)
        self.dob.update(innovation, dt_s)
        if self.rls is not None:
            self.rls.update(
                t_s=t_s,
                T_room_C=T_room_C,
                T_rad_C=float(x_hat[1]),
                T_outdoor_C=T_outdoor_C,
            )

        if t_s < self._next_mpc_t_s:
            return self._last_u, self._diagnostics()

        plant_delay_s = self.params.plant.valve_command_delay_s
        x_pred = self.smith.predict(
            x_hat, list(self._u_history), T_outdoor_C, plant_delay_s
        )

        u = self.optimiser.solve(
            x_pred=x_pred,
            T_sp=sp_for_opt,
            T_outdoor_C=T_outdoor_C,
            u_last=self._last_u,
            D_hat_K_per_min=self.dob.D_hat_K_per_min,
        )
        self.optimiser.update_integral(
            T_room=T_room_C, T_sp=sp_for_opt, u_applied=u, dt_s=self.params.qp.step_s
        )
        self._last_u = u
        self._u_history.append(u)
        self._next_mpc_t_s = t_s + self.params.qp.step_s

        return u, self._diagnostics()

    def export_snapshot(self) -> ControllerSnapshot:
        """Return a typed snapshot of the controller state for persistence."""
        return ControllerSnapshot(
            v=SNAPSHOT_VERSION,
            x_hat=[float(x) for x in self.kalman.x_hat],
            kalman_P=[[float(x) for x in row] for row in self.kalman.P],
            D_hat_K_per_min=self.dob.D_hat_K_per_min,
            last_u=self._last_u,
            e_integral_K_min=self.optimiser.e_integral_K_min,
            u_history=[float(u) for u in self._u_history],
            plant_params={
                "tau_room_min": self.plant_fine.params.tau_room_min,
                "tau_rad_min": self.plant_fine.params.tau_rad_min,
                "gain_heater": self.plant_fine.params.gain_heater,
                "coupling_rad_room": self.plant_fine.params.coupling_rad_room,
            },
            rg_v_C=self.governor.state(),
            last_t_s=self._last_t_s,
            next_mpc_t_s=self._next_mpc_t_s,
            rls=None if self.rls is None else self.rls.export_state(),
        )

    def restore_snapshot(self, snap: ControllerSnapshot) -> None:
        """Seed controller state from a snapshot, mutating sub-state in place.

        Empty estimate/covariance/history (a partial snapshot) leaves the
        freshly constructed defaults in place; version gating lives in
        :meth:`ControllerSnapshot.from_mapping`.
        """
        if snap.x_hat:
            self.kalman.initialise(np.asarray(snap.x_hat, dtype=float))
        if snap.kalman_P:
            self.kalman.P = np.asarray(snap.kalman_P, dtype=float)
        self.dob.D_hat_K_per_min = snap.D_hat_K_per_min
        self.optimiser.e_integral_K_min = snap.e_integral_K_min
        self._last_u = snap.last_u
        for name, value in snap.plant_params.items():
            if hasattr(self.plant_fine.params, name):
                setattr(self.plant_fine.params, name, value)
        for u in snap.u_history:
            self._u_history.append(u)
        self.governor.restore(snap.rg_v_C)
        self._last_t_s = snap.last_t_s
        self._next_mpc_t_s = snap.next_mpc_t_s
        if snap.rls is not None and self.rls is not None:
            self.rls.restore_state(snap.rls)
        self._initialised = True

    def set_applied_u(self, u: float) -> None:
        """Record the valve fraction actually applied to the TRV.

        When the caller clamps the command (e.g. ``max_opening_pct``), the
        Kalman observer, Smith predictor and rate limiter must see the applied
        value on the next cycle rather than the optimiser's uncapped request,
        otherwise their state drifts from the real plant input.

        Parameters
        ----------
        u : float
            Applied valve fraction in ``[0, 1]``.
        """
        u = max(0.0, min(1.0, u))
        self._last_u = u
        if self._u_history:
            self._u_history[-1] = u

    def _diagnostics(self) -> MpcV2Diagnostics:
        return MpcV2Diagnostics(
            T_room_hat=float(self.kalman.x_hat[0]),
            T_rad_hat=float(self.kalman.x_hat[1]),
            D_hat_K_per_min=self.dob.D_hat_K_per_min,
            rls_updates=self.rls.update_count if self.rls is not None else 0,
            rls_skips=self.rls.skip_count if self.rls is not None else 0,
            tau_room_min=self.plant_fine.params.tau_room_min,
            coupling_rad_room=self.plant_fine.params.coupling_rad_room,
        )
