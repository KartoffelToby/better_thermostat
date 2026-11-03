# pyright: reportConstantRedefinition=false
# Linear-algebra convention uses UPPERCASE names (P, Q, R, C) for mutable
# covariance / noise / measurement matrices. Strict pyright would otherwise
# treat them as ``Final``.
"""Kalman observer — reconstructs T_rad and the room's disturbance from T_room.

Most TRVs report the radiator temperature (``trv_temperature``) but Better
Thermostat treats the external room sensor as authoritative for the control
loop. The Kalman filter, linearised against the RC2 plant, takes only
``T_room`` as its measurement and reconstructs the unobserved ``T_rad`` from
the model dynamics. The TRV reading is not a filter input — it is used only to
seed the initial radiator estimate on the first cycle.

The third state is a disturbance rate on the room in K/min: the heat the plant
model misses (sun, occupants, a wrong prior). It follows a random walk and
enters the prediction through the room dynamics, so a learned disturbance
moves the predicted room the way it moves the real one. This is the
integrating disturbance model of offset-free MPC (Pannocchia & Rawlings,
AIChE J. 49(2), 2003): an estimator whose prediction ignores the disturbance
lets a biased model drag the estimate away from the room between readings.

Process noise on ``T_rad`` is intentionally larger than on ``T_room`` so
the filter adapts quickly to radiator dynamics the model gets wrong.

Each update propagates by the actual wall-clock spacing of control cycles.
The QP can still use a separate fixed coarse horizon, but the observer must
not interpret a five-minute measurement interval as one 30-second model step:
doing so biases the inferred radiator temperature and turns ordinary room
motion into a fictitious disturbance.

Room sensors report on a change (send-on-delta): a value repeats until the
room has moved by the sensor's reporting threshold. A repeated value is no
fresh measurement but the knowledge that the room is still within that
threshold of the last report. The filter then folds in that band by moment
matching on the truncated predicted reading (Sijs & Lazar, IEEE TAC 57(10),
2012; Wu et al., IEEE TAC 58(4), 2013): inside the band the estimate barely
moves, near its edge it is pushed back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

import numpy as np

from ._types import FloatArray
from .plant import PlantModelRC2
from .report_band import MIN_REPORT_THRESHOLD_K

# Bounds (in °C², the unit of the covariance entries) for the two invariants a
# stored covariance has to satisfy: symmetry and positive semi-definiteness.
# The recursion holds symmetry exactly and the JSON round-trip is lossless, so
# any measurable off-diagonal gap marks a corrupt payload. A genuinely
# semi-definite matrix, on the other hand, can produce a slightly negative
# smallest eigenvalue from the decomposition's own round-off.
_MAX_COVARIANCE_ASYMMETRY_C2 = 1e-9
_MIN_COVARIANCE_EIGENVALUE_C2 = -1e-9

# Below this probability mass the predicted reading lies so far outside the
# band that the truncated moments lose their precision; the band then acts
# as a reading at its nearer edge.
_MIN_BAND_MASS = 1e-12

# Order of the filter state: room, radiator, disturbance rate.
_STATE_DIM = 3
_PLANT_DIM = 2
_DISTURBANCE = 2


def _symmetric_part(P: FloatArray) -> FloatArray:
    """Return ``½·(P + Pᵀ)``, the symmetric part of a square matrix."""
    return (P + P.T) * 0.5


def _normal_pdf(z: float) -> float:
    """Return the standard normal density at ``z``."""
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _normal_mass(a: float, b: float) -> float:
    """Return the standard normal probability of ``[a, b]``.

    Above zero both distribution values approach 1 and their difference
    cancels; the upper tail ``½·erfc(z/√2)`` keeps the precision there.
    """
    if a > 0.0:
        return 0.5 * (math.erfc(a / math.sqrt(2.0)) - math.erfc(b / math.sqrt(2.0)))
    return 0.5 * (math.erfc(-b / math.sqrt(2.0)) - math.erfc(-a / math.sqrt(2.0)))


@dataclass
class KalmanParams:
    """Tunables for the discrete Kalman filter.

    The ``q_*`` entries are variance growths per nominal observer step
    (``plant_step_s``): °C² for the two temperatures, (K/min)² for the
    disturbance rate. ``q_disturbance`` holds for a sensor that reports every
    ``MIN_REPORT_THRESHOLD_K``; a coarser one scales it down with the square
    of its threshold, since it shows a change of the disturbance only that
    much later. ``p0_disturbance`` is the disturbance variance a fresh filter
    starts from, (K/min)².
    """

    q_room: float = 1e-3
    q_rad: float = 5e-3
    r_sensor: float = 1e-2
    q_disturbance: float = 3e-6
    p0_disturbance: float = 1e-4


class KalmanObserver:
    """Discrete Kalman filter against the linearised RC2 dynamics."""

    # Math-convention attribute names; declared here so strict type checkers
    # don't read the uppercase as ``Final`` and flag mutation.
    P: FloatArray
    Q: FloatArray
    R: FloatArray
    x_hat: FloatArray

    def __init__(
        self,
        plant: PlantModelRC2,
        params: KalmanParams,
        disturbance_bound_K_per_min: float = math.inf,
    ) -> None:
        """Initialise the filter state, covariance, and noise matrices.

        Seeds the estimate at ``[20, 20] °C`` without a disturbance, with a
        tight room / loose radiator initial covariance, and builds the
        process-noise (``Q``) and measurement-noise (``R``) matrices from
        ``params``.

        Parameters
        ----------
        plant : PlantModelRC2
            RC2 plant whose linearised dynamics drive the predict step.
        params : KalmanParams
            Process- and measurement-noise tunables for the filter.
        disturbance_bound_K_per_min : float, optional
            Largest disturbance rate the estimate may take, in either sign.
        """
        self.plant = plant
        self.params = params
        self.disturbance_bound_K_per_min = max(0.0, disturbance_bound_K_per_min)
        P0 = np.eye(_STATE_DIM)
        # Initial state-covariance variances (°C²). The room channel starts
        # tight (0.01 ⇒ ±0.1 °C) because it is directly measured; the radiator
        # channel keeps the looser unit prior (1.0 ⇒ ±1 °C) since it is only
        # reconstructed. Raise either to trust the seed less.
        P0[0, 0] = 0.01
        P0[_DISTURBANCE, _DISTURBANCE] = params.p0_disturbance
        self.P = P0
        # Room/radiator seed in °C — a neutral indoor guess; the first
        # measurement pulls the room state in immediately given the tight P0.
        self.x_hat = np.array([20.0, 20.0])
        # Estimated disturbance rate on the room (K/min).
        self.disturbance_K_per_min: float = 0.0
        self.Q = np.diag([params.q_room, params.q_rad, params.q_disturbance])
        self.R = np.array([[params.r_sensor]])

    def initialise(self, x0: FloatArray) -> None:
        """Seed the state estimate from ``x0`` and reset the covariance.

        The disturbance starts from zero; :meth:`restore_disturbance` adopts
        a persisted one.
        """
        x = np.asarray(x0, dtype=float).copy()
        self.x_hat = x[:_PLANT_DIM]
        self.disturbance_K_per_min = 0.0
        # Re-seed covariance (°C²): the room channel stays tight (0.01) since
        # ``x0`` carries a measured room temperature; the radiator channel gets
        # a moderate 0.5 prior because the seed there is only an estimate.
        P0 = np.eye(_STATE_DIM) * 0.5
        P0[0, 0] = 0.01
        P0[_DISTURBANCE, _DISTURBANCE] = self.params.p0_disturbance
        self.P = P0

    def restore_disturbance(self, rate_K_per_min: float) -> None:
        """Adopt a persisted disturbance rate, bounded like every estimate."""
        self.disturbance_K_per_min = self._bounded(rate_K_per_min)

    def update(
        self,
        y_meas: float,
        u: float,
        T_outdoor: float,
        dt_s: float | None = None,
        *,
        band_K: float | None = None,
        report_threshold_K: float = MIN_REPORT_THRESHOLD_K,
        learn_disturbance: bool = True,
    ) -> FloatArray:
        """Run one predict/correct step and return the updated state estimate.

        Predicts through the linearised RC2 dynamics for input ``u`` under the
        current disturbance, then corrects with the room reading ``y_meas``
        and returns a copy of the new ``[T_room, T_rad]`` estimate.

        Parameters
        ----------
        y_meas : float
            Room temperature the sensor last reported.
        u : float
            Valve fraction applied over the interval.
        T_outdoor : float
            Outdoor temperature over the interval.
        dt_s : float | None, optional
            Elapsed time since the previous update; one plant step when
            ``None``.
        band_K : float | None, optional
            ``None`` when ``y_meas`` is a fresh report. For a repeated report,
            the sensor's reporting threshold: the room is then known only to
            lie within ``y_meas ± band_K``.
        report_threshold_K : float, optional
            The sensor's reporting threshold, which scales the disturbance's
            process noise.
        learn_disturbance : bool, optional
            ``False`` holds the disturbance and its variance, so the
            correction moves only the two temperatures.
        """
        elapsed_s = self.plant.dt_s if dt_s is None else max(0.0, dt_s)
        F, x_pred = self._predict(u, T_outdoor, elapsed_s)
        # ``Q`` is configured for the plant's nominal observer step.  Scale
        # it with elapsed time so sparse events increase uncertainty instead
        # of making the filter over-confident. The prediction covers at most
        # the plant's settling time, and so does the process noise: the
        # stable dynamics forget older disturbances just as they forget the
        # older state, so a longer silence adds no further uncertainty.
        q_scale = min(elapsed_s, self.plant.settling_time_s) / max(
            self.plant.dt_s, 1e-9
        )
        Q = self.Q * q_scale
        Q[_DISTURBANCE, _DISTURBANCE] *= (
            MIN_REPORT_THRESHOLD_K / max(report_threshold_K, MIN_REPORT_THRESHOLD_K)
        ) ** 2
        P_pred = F @ self.P @ F.T + Q
        if not learn_disturbance:
            # Without cross-covariance the gain on the disturbance is zero,
            # so the correction below leaves it and its variance as they are.
            P_pred[_DISTURBANCE, :] = 0.0
            P_pred[:, _DISTURBANCE] = 0.0
            P_pred[_DISTURBANCE, _DISTURBANCE] = self.P[_DISTURBANCE, _DISTURBANCE]

        y_pred = float(x_pred[0])
        # ``S`` is the variance of the predicted reading; a non-positive one
        # (from a corrupted, restored covariance) is floored so the gain
        # cannot blow up.
        s = max(float(P_pred[0, 0] + self.R[0, 0]), 1e-12)
        shift, variance = self._reading_moments(y_meas, y_pred, s, band_K)
        gain = P_pred[:, 0] / s
        x_new = x_pred + gain * shift
        # ``P_pred − P_pred·Cᵀ·C·P_pred·(S − v)/S²`` is symmetric in exact
        # arithmetic only; the floating-point product drifts a little off the
        # diagonal every step. Keeping just the symmetric part holds the
        # invariant exactly, so the covariance that gets persisted is still a
        # covariance on restore.
        self.P = _symmetric_part(
            P_pred - np.outer(P_pred[:, 0], P_pred[:, 0]) * (s - variance) / (s * s)
        )
        self.x_hat = x_new[:_PLANT_DIM].copy()
        self.disturbance_K_per_min = self._bounded(float(x_new[_DISTURBANCE]))
        return self.x_hat.copy()

    @staticmethod
    def _reading_moments(
        y_meas: float, y_pred: float, s: float, band_K: float | None
    ) -> tuple[float, float]:
        """Return how far the reading moves the prediction, and what is left of ``S``.

        A fresh report is a point measurement: the shift is the innovation and
        no variance of the predicted reading remains. A repeated report is the
        band ``y_meas ± band_K``: the predicted reading ``N(y_pred, S)``
        truncated to it gives the shift (mean minus ``y_pred``) and the
        remaining variance.
        """
        if band_K is None:
            return y_meas - y_pred, 0.0
        lower, upper = y_meas - band_K, y_meas + band_K
        sd = math.sqrt(s)
        a = (lower - y_pred) / sd
        b = (upper - y_pred) / sd
        mass = _normal_mass(a, b)
        if mass < _MIN_BAND_MASS:
            edge = lower if a > 0.0 else upper
            return edge - y_pred, 0.0
        pdf_a, pdf_b = _normal_pdf(a), _normal_pdf(b)
        mean_z = (pdf_a - pdf_b) / mass
        variance = s * (1.0 + (a * pdf_a - b * pdf_b) / mass - mean_z * mean_z)
        return sd * mean_z, max(0.0, variance)

    def _bounded(self, rate_K_per_min: float) -> float:
        bound = self.disturbance_bound_K_per_min
        return max(-bound, min(bound, rate_K_per_min))

    def restore_covariance(self, stored: Sequence[Sequence[float]]) -> bool:
        """Adopt a persisted covariance and report whether it was usable.

        A matrix qualifies when it is square in the filter's dimension,
        finite, symmetric and positive semi-definite. An indefinite one makes
        the innovation variance ``S`` negative; the floor in :meth:`update`
        then yields a gain around ``1e12`` and the state estimate diverges
        within a few cycles. A rejected matrix leaves the current covariance
        untouched, so the observer keeps its construction default and simply
        re-learns.

        A 2×2 matrix is the covariance of the two temperatures alone, as a
        filter without a disturbance state stored it. It keeps the current
        disturbance variance, uncorrelated with the temperatures.

        Parameters
        ----------
        stored : Sequence[Sequence[float]]
            Covariance rows as read back from persistent storage.

        Returns
        -------
        bool
            ``True`` when the covariance was adopted.
        """
        try:
            P = np.asarray(stored, dtype=float)
        except TypeError, ValueError:
            return False
        if P.shape == (_PLANT_DIM, _PLANT_DIM):
            full = np.zeros((_STATE_DIM, _STATE_DIM))
            full[:_PLANT_DIM, :_PLANT_DIM] = P
            full[_DISTURBANCE, _DISTURBANCE] = self.P[_DISTURBANCE, _DISTURBANCE]
            P = full
        if P.shape != self.P.shape or not bool(np.all(np.isfinite(P))):
            return False
        symmetric = _symmetric_part(P)
        if float(np.max(np.abs(P - symmetric))) > _MAX_COVARIANCE_ASYMMETRY_C2:
            return False
        if float(np.min(np.linalg.eigvalsh(symmetric))) < _MIN_COVARIANCE_EIGENVALUE_C2:
            return False
        self.P = symmetric
        return True

    def _predict(
        self, u: float, T_outdoor: float, elapsed_s: float
    ) -> tuple[FloatArray, FloatArray]:
        """Return the state transition ``F`` and the predicted state.

        The temperatures follow the plant's own sub-stepped dynamics, whose
        valve drive shrinks as the radiator approaches the supply water, so no
        interval between readings carries the estimate past it. The
        disturbance holds over the interval and acts on the room through the
        same dynamics; since they are affine in it, the difference against a
        unit-higher rate is its column of ``F``. The temperature block of
        ``F`` does not depend on the operating point and propagates the
        covariance.
        """
        A, _, _ = self.plant.linearised_system(
            T_outdoor, float(self.x_hat[1]), dt_s=elapsed_s
        )
        rate = self.disturbance_K_per_min
        x_pred = self.plant.propagate(self.x_hat, u, T_outdoor, elapsed_s, rate)
        x_unit = self.plant.propagate(self.x_hat, u, T_outdoor, elapsed_s, rate + 1.0)
        F = np.eye(_STATE_DIM)
        F[:_PLANT_DIM, :_PLANT_DIM] = A
        F[:_PLANT_DIM, _DISTURBANCE] = x_unit - x_pred
        return F, np.append(x_pred, rate)
