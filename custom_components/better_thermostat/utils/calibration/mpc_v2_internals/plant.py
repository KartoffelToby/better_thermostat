"""RC2 plant model — continuous-time dynamics + ZOH discretisation.

The state is ``x = [T_room, T_rad]`` and the dynamics are

    τ_room · dT_room/dt = coupling · (T_rad − T_room) − (T_room − T_outdoor)
    τ_rad  · dT_rad/dt  = gain · u · (T_water − T_rad) − (T_rad − T_room)

with ``u ∈ [0,1]`` the valve fraction. The class supplies a one-step Euler
integrator (used by the Smith predictor and the reference-governor sim) and
a linearised (A, B, d) discrete-time system used by Kalman and the QP.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ._types import FloatArray


@dataclass
class PlantParams:
    """Lumped-RC parameters for a single radiator+room thermal mass.

    Defaults model a representative residential envelope (τ_room ≈ 8 h).
    """

    tau_room_min: float = 480.0
    tau_rad_min: float = 15.0
    gain_heater: float = 2.0
    coupling_rad_room: float = 1.0
    T_water_C: float = 65.0
    valve_command_delay_s: float = 0.0


class PlantModelRC2:
    """Continuous RC2 plant + zero-order-hold discretisation."""

    def __init__(self, params: PlantParams, dt_s: float) -> None:
        """Bind the lumped-RC parameters and cache the step duration.

        Parameters
        ----------
        params : PlantParams
            Lumped-RC parameters describing the radiator+room thermal mass.
        dt_s : float
            Discretisation step in seconds; also cached in minutes as
            ``dt_min`` for the per-minute thermal time constants.
        """
        self.params = params
        self.dt_s = dt_s
        self.dt_min = dt_s / 60.0

    @property
    def state_dim(self) -> int:
        """Number of state variables (``T_room`` and ``T_rad``)."""
        return 2

    def discrete_step(
        self, x: FloatArray, u: float, T_outdoor_C: float, D_K_per_min: float = 0.0
    ) -> FloatArray:
        """Forward-Euler one-step propagator."""
        p = self.params
        u_clamped = max(0.0, min(1.0, u))
        T_room, T_rad = float(x[0]), float(x[1])
        dT_rad = (
            p.gain_heater * u_clamped * (p.T_water_C - T_rad) - (T_rad - T_room)
        ) / p.tau_rad_min
        dT_room = (
            p.coupling_rad_room * (T_rad - T_room) - (T_room - T_outdoor_C)
        ) / p.tau_room_min
        return np.array(
            [
                T_room + (dT_room + D_K_per_min) * self.dt_min,
                T_rad + dT_rad * self.dt_min,
            ]
        )

    def linearised_AB(
        self, T_outdoor_C: float, T_rad_op_C: float
    ) -> tuple[FloatArray, FloatArray, FloatArray]:
        """Return ``(A, B, d)`` for ``x_{k+1} = A·x + B·u + d``.

        The Jacobian is constant in ``x`` (the plant is linear) but ``B``
        depends on the radiator operating point because of the bilinear
        ``u·(T_water − T_rad)`` term — we evaluate at ``T_rad_op_C``.
        """
        p = self.params
        dt_min = self.dt_min
        a_rad_room = dt_min / p.tau_rad_min
        a_rad_rad = 1.0 - dt_min / p.tau_rad_min
        b_rad = dt_min * p.gain_heater * (p.T_water_C - T_rad_op_C) / p.tau_rad_min
        a_room_room = 1.0 - dt_min * (p.coupling_rad_room + 1.0) / p.tau_room_min
        a_room_rad = dt_min * p.coupling_rad_room / p.tau_room_min
        d_room = dt_min * T_outdoor_C / p.tau_room_min
        A = np.array([[a_room_room, a_room_rad], [a_rad_room, a_rad_rad]])
        B = np.array([[0.0], [b_rad]])
        d = np.array([d_room, 0.0])
        return A, B, d

    def steady_radiator_temp(
        self, T_setpoint_C: float, T_outdoor_C: float, D_hat_K_per_min: float = 0.0
    ) -> float:
        """Return the radiator temperature that holds ``T_setpoint_C`` at steady state.

        From the room heat-loss balance: the loss the radiator must cover is
        ``(T_setpoint − T_outdoor) − D̂·τ_room``, and dividing by the radiator↔room
        coupling gives the radiator overshoot above setpoint.
        """
        p = self.params
        loss_K = (T_setpoint_C - T_outdoor_C) - D_hat_K_per_min * p.tau_room_min
        return T_setpoint_C + loss_K / max(p.coupling_rad_room, 1e-6)

    def steady_input(
        self, T_setpoint_C: float, T_outdoor_C: float, D_hat_K_per_min: float = 0.0
    ) -> float:
        """Return the valve fraction that holds ``T_setpoint_C`` at steady state.

        Solves the RC2 fixed point: the heat-loss balance fixes the radiator
        temperature, which fixes the valve fraction. The result is **not**
        clamped to ``[0, 1]`` — callers saturate it or test it for feasibility.

        Parameters
        ----------
        T_setpoint_C : float
            Target room temperature.
        T_outdoor_C : float
            Outdoor temperature driving the loss term.
        D_hat_K_per_min : float, optional
            Estimated lumped disturbance (K/min); subtracted from the loss.

        Returns
        -------
        float
            Steady-state valve fraction (unclamped).
        """
        p = self.params
        T_rad_ss = self.steady_radiator_temp(T_setpoint_C, T_outdoor_C, D_hat_K_per_min)
        denom = max(p.gain_heater * (p.T_water_C - T_rad_ss), 1e-6)
        return (T_rad_ss - T_setpoint_C) / denom
