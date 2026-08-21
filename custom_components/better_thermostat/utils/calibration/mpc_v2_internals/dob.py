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
    # A single quantised room-sensor jump must not become an arbitrarily large
    # permanent heat source/sink in the steady-state feed-forward term.  0.05
    # K/min is already 3 K/hour, well beyond a normal unmodelled room load.
    max_abs_K_per_min: float = 0.05


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

        The estimate itself is bounded by ``max_abs_K_per_min`` so a quantised
        sensor jump cannot become an implausible steady-state load. That bound
        belongs on the estimate rather than on the incoming rate: clamping the
        rate first would scale it by the dt-proportional weight as well, which
        drops the short-interval innovations this observer is meant to fold in.
        """
        if dt_s <= 0.0:
            return self.D_hat_K_per_min
        innov_rate = innovation_K / (dt_s / 60.0)
        a = min(1.0, dt_s / max(self.params.tau_s, dt_s))
        max_abs = max(0.0, self.params.max_abs_K_per_min)
        self.D_hat_K_per_min = (1.0 - a) * self.D_hat_K_per_min + a * innov_rate
        self.D_hat_K_per_min = max(-max_abs, min(max_abs, self.D_hat_K_per_min))
        return self.D_hat_K_per_min
