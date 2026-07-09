# pyright: reportConstantRedefinition=false
# Linear-algebra convention uses UPPERCASE names (P, Q, R, C) for mutable
# covariance / noise / measurement matrices. Strict pyright would otherwise
# treat them as ``Final``.
"""2-state Kalman observer — reconstructs T_rad from T_room measurements.

Most TRVs report the radiator temperature (``trv_temp_C``) but Better
Thermostat treats the external room sensor as authoritative for the control
loop. The Kalman filter, linearised against the RC2 plant, takes only
``T_room`` as its measurement (``C = [[1, 0]]``) and reconstructs the
unobserved ``T_rad`` from the model dynamics. The TRV reading is not a filter
input — it is used only to seed the initial radiator estimate on the first
cycle.

Process noise on ``T_rad`` is intentionally larger than on ``T_room`` so
the filter adapts quickly to radiator dynamics the model gets wrong.

Each update propagates exactly one plant step (``plant_step_s``) regardless
of the actual wall-clock spacing of control cycles. For the slow thermal
plant this mis-weights the model dynamics between irregular cycles, but the
measurement correction on every cycle keeps the room channel anchored; the
controller additionally rejects sub-second repeat steps so the same
measurement is never folded in twice within one control pass.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ._types import FloatArray
from .plant import PlantModelRC2


@dataclass
class KalmanParams:
    """Tunables for the discrete Kalman filter."""

    q_room: float = 1e-3
    q_rad: float = 5e-3
    r_sensor: float = 1e-2


class KalmanObserver:
    """Discrete Kalman filter against the linearised RC2 dynamics."""

    # Math-convention attribute names; declared here so strict type checkers
    # don't read the uppercase as ``Final`` and flag mutation.
    P: FloatArray
    Q: FloatArray
    R: FloatArray
    C: FloatArray
    x_hat: FloatArray

    def __init__(self, plant: PlantModelRC2, params: KalmanParams) -> None:
        """Initialise the filter state, covariance, and noise matrices.

        Seeds the estimate at ``[20, 20] °C`` with a tight room / loose
        radiator initial covariance and builds the process-noise (``Q``),
        measurement-noise (``R``), and output (``C``) matrices from ``params``.

        Parameters
        ----------
        plant : PlantModelRC2
            RC2 plant whose linearised dynamics drive the predict step.
        params : KalmanParams
            Process- and measurement-noise tunables for the filter.
        """
        self.plant = plant
        self.params = params
        P0 = np.eye(2)
        # Initial state-covariance variances (°C²). The room channel starts
        # tight (0.01 ⇒ ±0.1 °C) because it is directly measured; the radiator
        # channel keeps the looser unit prior (1.0 ⇒ ±1 °C) since it is only
        # reconstructed. Raise either to trust the seed less.
        P0[0, 0] = 0.01
        self.P = P0
        # Room/radiator seed in °C — a neutral indoor guess; the first
        # measurement pulls the room state in immediately given the tight P0.
        self.x_hat = np.array([20.0, 20.0])
        self.Q = np.diag([params.q_room, params.q_rad])
        self.R = np.array([[params.r_sensor]])
        self.C = np.array([[1.0, 0.0]])

    def initialise(self, x0: FloatArray) -> None:
        """Seed the state estimate from ``x0`` and reset the covariance."""
        x = np.asarray(x0, dtype=float).copy()
        self.x_hat = x[:2]
        # Re-seed covariance (°C²): the room channel stays tight (0.01) since
        # ``x0`` carries a measured room temperature; the radiator channel gets
        # a moderate 0.5 prior because the seed there is only an estimate.
        P0 = np.eye(2) * 0.5
        P0[0, 0] = 0.01
        self.P = P0

    def update(self, y_meas: float, u: float, T_outdoor_C: float) -> FloatArray:
        """Run one predict/correct step and return the updated state estimate.

        Predicts through the linearised RC2 dynamics for input ``u``, then
        corrects with the room measurement ``y_meas`` and returns a copy of the
        new ``[T_room, T_rad]`` estimate.
        """
        A, B, d = self.plant.linearised_AB(T_outdoor_C, float(self.x_hat[1]))
        x_pred = A @ self.x_hat + B.flatten() * u + d
        P_pred = A @ self.P @ A.T + self.Q
        innovation = y_meas - float((self.C @ x_pred).item())
        # ``S = C·P_pred·Cᵀ + R`` is 1×1; invert it as a guarded scalar
        # reciprocal so a corrupted (e.g. restored) covariance can't drive a
        # singular matrix through ``np.linalg.inv``.
        s = float((self.C @ P_pred @ self.C.T + self.R)[0, 0])
        K = P_pred @ self.C.T * (1.0 / max(s, 1e-12))
        self.x_hat = x_pred + (K.flatten() * innovation)
        self.P = (np.eye(2) - K @ self.C) @ P_pred
        return self.x_hat.copy()

    def innovation(self, y_meas: float, u: float, T_outdoor_C: float) -> float:
        """Pre-update residual — used by the disturbance observer."""
        A, B, d = self.plant.linearised_AB(T_outdoor_C, float(self.x_hat[1]))
        x_pred = A @ self.x_hat + B.flatten() * u + d
        return y_meas - float((self.C @ x_pred).item())
