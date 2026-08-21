"""MPC v2 controller stack and its persistence snapshot."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
import math
from typing import Any

import numpy as np

from ..mpc_v2_internals.dob import DisturbanceObserver
from ..mpc_v2_internals.governor import ScalarReferenceGovernor
from ..mpc_v2_internals.kalman import KalmanObserver
from ..mpc_v2_internals.plant import PlantModelRC2
from ..mpc_v2_internals.qp_optimiser import QpOptimiser
from ..mpc_v2_internals.smith import SmithPredictor
from .io import MpcV2Diagnostics
from .params import MpcV2Params

_LOGGER = logging.getLogger(__name__)

# Snapshot format version. Bump when adding/renaming persisted fields so
# restore_snapshot can refuse payloads from a future Better Thermostat
# release. Pre-versioning snapshots are treated as version 0.
SNAPSHOT_VERSION = 2

# Steps arriving closer together than this carry no new information: in a
# multi-TRV group every TRV dispatch steps the same shared controller within
# one control pass, milliseconds apart. Re-folding the same room measurement
# would shrink the Kalman covariance without evidence, so those repeat steps
# return the last command unchanged.
MIN_STEP_DT_S = 1.0


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
    rg_v_C: float | None
    last_t_s: float
    next_mpc_t_s: float
    last_mpc_t_s: float = -1.0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ControllerSnapshot | None:
        """Parse a persisted mapping; ``None`` for a future version or bad data.

        This is the single place raw (untyped) persisted data is validated and
        coerced; missing keys fall back to the controller's construction
        defaults, so a partial snapshot degrades gracefully. A snapshot with
        non-numeric or non-finite values is dropped entirely — ``float`` accepts
        ``NaN`` and infinity, and either one spreads through the observer into
        every later command, so the controller boots fresh instead of running on
        poisoned state. ``rg_v_C`` is the one nullable field: a stored ``null``
        means "no governor state" and stays legal.
        """
        try:
            version = int(raw.get("v", 0))
            if version > SNAPSHOT_VERSION:
                _LOGGER.warning(
                    "MPC v2 snapshot version %d > supported %d; ignoring",
                    version,
                    SNAPSHOT_VERSION,
                )
                return None
            snapshot = cls(
                v=version,
                x_hat=[float(x) for x in raw.get("x_hat", [])],
                kalman_P=[[float(x) for x in row] for row in raw.get("kalman_P", [])],
                D_hat_K_per_min=float(raw.get("D_hat_K_per_min", 0.0)),
                last_u=float(raw.get("last_u", 0.0)),
                e_integral_K_min=float(raw.get("e_integral_K_min", 0.0)),
                u_history=[float(x) for x in raw.get("u_history", [])],
                rg_v_C=None if raw.get("rg_v_C") is None else float(raw["rg_v_C"]),
                last_t_s=float(raw.get("last_t_s", 0.0)),
                next_mpc_t_s=float(raw.get("next_mpc_t_s", -1.0)),
                last_mpc_t_s=float(raw.get("last_mpc_t_s", -1.0)),
            )
        except TypeError, ValueError, OverflowError:
            _LOGGER.warning("MPC v2 snapshot contains non-numeric data; ignoring")
            return None
        numbers = [
            *snapshot.x_hat,
            *(x for row in snapshot.kalman_P for x in row),
            *snapshot.u_history,
            snapshot.D_hat_K_per_min,
            snapshot.last_u,
            snapshot.e_integral_K_min,
            snapshot.last_t_s,
            snapshot.next_mpc_t_s,
            snapshot.last_mpc_t_s,
            *([] if snapshot.rg_v_C is None else [snapshot.rg_v_C]),
        ]
        if not all(math.isfinite(x) for x in numbers):
            _LOGGER.warning("MPC v2 snapshot contains non-finite data; ignoring")
            return None
        return snapshot


class MpcV2Controller:
    """Glue: plant + Kalman + Smith + DOB + governor + QP.

    Lifetime is one room. The plant params are a fixed prior for the
    controller's lifetime; rehydration from persistent state goes through
    :meth:`export_snapshot` / :meth:`restore_snapshot`.
    """

    def __init__(self, mpc_params: MpcV2Params) -> None:
        """Build the controller stack (plant, observers, optimiser, governor).

        Parameters
        ----------
        mpc_params : MpcV2Params
            Plant prior plus observer/optimiser/governor tunables. The ``qp``
            sub-params are copied because ``step_s`` is rewritten below, so the
            caller's object is never mutated.
        """
        # Copy the one sub-param this controller mutates in place — ``qp``
        # (``step_s`` rewritten below) — so the caller's object is never
        # aliased. It is a flat dataclass, so a shallow ``replace`` fully
        # isolates it; the read-only plant/kalman/dob/governor params are
        # shared by reference.
        mpc_params = replace(mpc_params, qp=replace(mpc_params.qp))
        self.params = mpc_params
        # Plant-aware MPC step: faster envelopes ⇒ finer grid. One-shot at
        # construction so the QP workspace is built once.
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
        # The Smith predictor replays the command history, which holds one
        # entry per MPC re-plan — its time base is the QP step, so it runs
        # on the coarse plant, not the fine observer plant.
        self.smith = SmithPredictor(self.plant_coarse)
        self.dob = DisturbanceObserver(mpc_params.dob)
        self.optimiser = QpOptimiser(self.plant_coarse, mpc_params.qp)
        self.governor = ScalarReferenceGovernor(self.plant_coarse, mpc_params.governor)

        self._u_history: deque[float] = deque(maxlen=64)
        self._last_u: float = 0.0
        self._last_t_s: float = 0.0
        self._next_mpc_t_s: float = -1.0
        self._last_mpc_t_s: float = -1.0
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
            by the disturbance observer and the MPC re-plan cadence.
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
        if self._last_t_s > 0 and dt_s < MIN_STEP_DT_S:
            # Forward-only: a non-positive dt_s (backward time jump) or a step
            # below the minimum reuses the previous state and must NOT advance
            # _last_t_s, otherwise a stale timestamp would reach dob.update.
            return self._last_u, self._diagnostics()
        self._last_t_s = t_s

        sp_for_opt = self.governor.update(
            T_sp=T_target_C, T_outdoor_C=T_outdoor_C, T_room_now=T_room_C
        )

        # The observer follows real elapsed time.  The QP below intentionally
        # remains on its fixed coarse planning grid; mixing those two time
        # bases was the source of large artificial DOB excursions on sparse
        # (typically five-minute) Home Assistant updates.
        innovation = self.kalman.innovation(
            T_room_C, self._last_u, T_outdoor_C, dt_s=dt_s
        )
        x_hat = self.kalman.update(T_room_C, self._last_u, T_outdoor_C, dt_s=dt_s)
        self.dob.update(innovation, dt_s)

        if t_s < self._next_mpc_t_s:
            return self._last_u, self._diagnostics()

        plant_delay_s = self.params.plant.valve_command_delay_s
        x_pred = self.smith.predict(
            x_hat, list(self._u_history), T_outdoor_C, plant_delay_s
        )

        # Account for the time the previous valve input was actually in
        # effect.  The first plan has no preceding control interval.
        if self._last_mpc_t_s >= 0.0:
            self.optimiser.update_integral(
                T_room=T_room_C,
                T_sp=sp_for_opt,
                u_applied=self._last_u,
                dt_s=max(0.0, t_s - self._last_mpc_t_s),
            )

        u = self.optimiser.solve(
            x_pred=x_pred,
            T_sp=sp_for_opt,
            T_outdoor_C=T_outdoor_C,
            u_last=self._last_u,
            D_hat_K_per_min=self.dob.D_hat_K_per_min,
        )
        self._last_u = u
        self._u_history.append(u)
        self._last_mpc_t_s = t_s
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
            rg_v_C=self.governor.state(),
            last_t_s=self._last_t_s,
            next_mpc_t_s=self._next_mpc_t_s,
            last_mpc_t_s=self._last_mpc_t_s,
        )

    def restore_snapshot(self, snap: ControllerSnapshot) -> None:
        """Seed controller state from a snapshot, mutating sub-state in place.

        An estimate or covariance that is empty, wrong-shaped or non-finite (a
        partial or corrupted snapshot) leaves the freshly constructed defaults
        in place — a mis-shaped or ``NaN`` covariance would otherwise poison
        every subsequent Kalman update. Version gating lives in
        :meth:`ControllerSnapshot.from_mapping`.
        """
        n = self.plant_fine.state_dim
        x_hat = np.asarray(snap.x_hat, dtype=float)
        seeded = x_hat.shape == (n,) and bool(np.all(np.isfinite(x_hat)))
        if seeded:
            self.kalman.initialise(x_hat)
        if snap.kalman_P:
            P = np.asarray(snap.kalman_P, dtype=float)
            if P.shape == (n, n) and bool(np.all(np.isfinite(P))):
                self.kalman.P = P
        self.dob.D_hat_K_per_min = snap.D_hat_K_per_min
        self.optimiser.e_integral_K_min = snap.e_integral_K_min
        self._last_u = snap.last_u
        for u in snap.u_history:
            self._u_history.append(u)
        self.governor.restore(snap.rg_v_C)
        self._last_t_s = snap.last_t_s
        self._next_mpc_t_s = snap.next_mpc_t_s
        self._last_mpc_t_s = snap.last_mpc_t_s
        # The controller counts as initialised only when the snapshot carried a
        # usable estimate. Without one the Kalman filter still holds its
        # construction default, so the first :meth:`step` has to seed it from
        # the measurement instead of treating the default as restored state.
        self._initialised = seeded

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

    def set_command_u(self, u: float) -> None:
        """Record this cycle's bounded command pending device confirmation."""
        self._last_u = max(0.0, min(1.0, u))
        if self._u_history:
            self._u_history[-1] = self._last_u

    def _diagnostics(self) -> MpcV2Diagnostics:
        return MpcV2Diagnostics(
            T_room_hat=float(self.kalman.x_hat[0]),
            T_rad_hat=float(self.kalman.x_hat[1]),
            D_hat_K_per_min=self.dob.D_hat_K_per_min,
            tau_room_min=self.plant_fine.params.tau_room_min,
            coupling_rad_room=self.plant_fine.params.coupling_rad_room,
        )
