"""Disturbance observer — EMA over Kalman innovations.

The QP's steady-state input ``u_ss`` assumes the plant model is exact. Real
rooms see unmodelled disturbances (open windows, solar gain, occupants);
the DOB captures the average rate in ``K/min`` so ``_steady_input_for`` can
feed-forward against it. Without the DOB, integral-only correction would
leave a slow setpoint offset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DobParams:
    """Tunables for the disturbance observer (EMA time constant)."""

    tau_s: float = 600.0


class DisturbanceObserver:
    """EMA over Kalman innovations estimating the disturbance rate in K/min."""

    def __init__(self, params: DobParams) -> None:
        """Initialise the observer with a zero disturbance estimate.

        Parameters
        ----------
        params : DobParams
            Tunables for the observer, notably the EMA time constant ``tau_s``.
        """
        self.params = params
        self.D_hat_K_per_min: float = 0.0

    def update(self, innovation_K: float, dt_s: float) -> float:
        """Fold one innovation into the EMA and return the disturbance estimate.

        Converts the per-step innovation into a ``K/min`` rate and blends it
        with EMA weight ``a`` derived from ``dt_s`` and ``tau_s``. Non-positive
        ``dt_s`` leaves the current estimate unchanged.

        The weight scales linearly with ``dt_s`` (no lower floor): the
        innovation rate grows as ``1/dt_s``, so a dt-proportional weight keeps
        the per-update contribution ``a * innov_rate`` bounded by
        ``60 * innovation_K / tau_s`` even for near-zero intervals, as they
        occur when a shared group controller is stepped once per TRV within
        the same control pass.
        """
        if dt_s <= 0.0:
            return self.D_hat_K_per_min
        innov_rate = innovation_K / (dt_s / 60.0)
        a = min(1.0, dt_s / max(self.params.tau_s, dt_s))
        self.D_hat_K_per_min = (1.0 - a) * self.D_hat_K_per_min + a * innov_rate
        return self.D_hat_K_per_min
