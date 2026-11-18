"""Multi-zone plant model.

Every radiator heats an air zone of its own. The zones lose heat to the
outdoors in different shares and exchange heat with each other, so a room can
hold a cold corner next to a warm one. A room whose zones exchange heat without
limit behaves like the single room node of :class:`MultiTrvPlant`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

# The integrator's longest step. Strong air exchange makes the zones the
# fastest states of the plant.
_MAX_SUBSTEP_S = 10.0


@dataclass(frozen=True)
class MultiZonePlantParams:
    """Thermal parameters for one radiator per air zone.

    Attributes
    ----------
    tau_room_min : float
        Time constant of the whole room against the outdoors, in minutes.
    tau_rad_min : float
        Time constant of a radiator, in minutes.
    gain_heaters : tuple[float, ...]
        Water-side gain of each radiator.
    coupling_rad_zone : tuple[float, ...]
        Conductance from each radiator to its zone, relative to the room's
        loss to the outdoors.
    loss_shares : tuple[float, ...]
        Share of the room's loss to the outdoors each zone carries.
    air_exchange : float
        Conductance between two zones, relative to the room's loss to the
        outdoors.
    T_water : float
        Flow temperature in °C.
    """

    tau_room_min: float = 480.0
    tau_rad_min: float = 15.0
    gain_heaters: tuple[float, ...] = (3.0, 3.0)
    coupling_rad_zone: tuple[float, ...] = (1.5, 1.5)
    loss_shares: tuple[float, ...] = (0.5, 0.5)
    air_exchange: float = 4.0
    T_water: float = 65.0

    def __post_init__(self) -> None:
        """Reject parameters the plant cannot integrate.

        Raises
        ------
        ValueError
            If the per-zone tuples differ in length, a time constant is not
            positive, the loss shares do not sum to 1, or the air exchange is
            negative.
        """
        n_zones = len(self.gain_heaters)
        if n_zones < 2:
            raise ValueError(f"a multi-zone plant needs two zones, got {n_zones}")
        if {len(self.coupling_rad_zone), len(self.loss_shares)} != {n_zones}:
            raise ValueError(
                "gain_heaters, coupling_rad_zone and loss_shares must have one "
                "entry per zone"
            )
        if self.tau_room_min <= 0.0 or self.tau_rad_min <= 0.0:
            raise ValueError("tau_room_min and tau_rad_min must be > 0")
        if not math.isclose(sum(self.loss_shares), 1.0, abs_tol=1e-9):
            raise ValueError(f"loss_shares must sum to 1, got {self.loss_shares}")
        if self.air_exchange < 0.0:
            raise ValueError(f"air_exchange must be >= 0, got {self.air_exchange}")

    @property
    def n_zones(self) -> int:
        """Number of air zones, each with one radiator."""
        return len(self.gain_heaters)


@dataclass
class MultiZonePlantState:
    """Mutable simulator state of a multi-zone plant."""

    T_zones: list[float]
    T_rads: list[float] = field(default_factory=list)


class MultiZonePlant:
    """Radiators heating air zones that exchange heat with each other."""

    def __init__(
        self, params: MultiZonePlantParams, initial: MultiZonePlantState
    ) -> None:
        if {len(initial.T_zones), len(initial.T_rads)} != {params.n_zones}:
            raise ValueError(
                f"initial state must have {params.n_zones} zones and radiators"
            )
        self.params = params
        self.state = MultiZonePlantState(
            T_zones=list(initial.T_zones), T_rads=list(initial.T_rads)
        )

    @property
    def heat_to_room_K_per_min(self) -> float:
        """Heat all radiators currently deliver to the room, in room-K/min."""
        p = self.params
        s = self.state
        return (
            sum(
                p.coupling_rad_zone[i] * (s.T_rads[i] - s.T_zones[i])
                for i in range(p.n_zones)
            )
            / p.tau_room_min
        )

    def step(
        self,
        dt_s: float,
        u_per_zone: list[float],
        T_outdoor: float,
        Q_K_per_min: float = 0.0,
        flow_temperature: float | None = None,
        heat_delivered: bool = True,
    ) -> MultiZonePlantState:
        """Advance the plant by ``dt_s`` seconds.

        Parameters
        ----------
        dt_s : float
            Step in seconds.
        u_per_zone : list[float]
            Valve opening in [0, 1] per radiator.
        T_outdoor : float
            Outdoor temperature in °C.
        Q_K_per_min : float
            Disturbance acting on every zone alike, in K/min.
        flow_temperature : float or None
            Flow temperature replacing ``T_water``.
        heat_delivered : bool
            ``False`` while the heat source delivers nothing.

        Returns
        -------
        MultiZonePlantState
            The state after the step.
        """
        if dt_s <= 0.0:
            return self.state
        if len(u_per_zone) != self.params.n_zones:
            raise ValueError(
                f"u_per_zone has {len(u_per_zone)} entries, "
                f"expected {self.params.n_zones}"
            )
        T_water = self.params.T_water if flow_temperature is None else flow_temperature
        substeps = math.ceil(dt_s / _MAX_SUBSTEP_S)
        for _ in range(substeps):
            self._integrate(
                dt_s / substeps / 60.0,
                u_per_zone,
                T_outdoor,
                Q_K_per_min,
                T_water,
                heat_delivered,
            )
        return self.state

    def _integrate(
        self,
        dt_min: float,
        u_per_zone: list[float],
        T_outdoor: float,
        Q_K_per_min: float,
        T_water: float,
        heat_delivered: bool,
    ) -> None:
        """Advance the plant by one explicit Euler step of ``dt_min`` minutes."""
        p = self.params
        s = self.state
        tau_zone_min = p.tau_room_min / p.n_zones
        dT_rads = [0.0] * p.n_zones
        dT_zones = [0.0] * p.n_zones
        for i in range(p.n_zones):
            u_i = max(0.0, min(1.0, u_per_zone[i]))
            heat_in = (
                p.gain_heaters[i] * u_i * (T_water - s.T_rads[i])
                if heat_delivered
                else 0.0
            )
            dT_rads[i] = (heat_in - (s.T_rads[i] - s.T_zones[i])) / p.tau_rad_min
            exchanged = sum(s.T_zones[j] - s.T_zones[i] for j in range(p.n_zones))
            dT_zones[i] = (
                p.coupling_rad_zone[i] * (s.T_rads[i] - s.T_zones[i])
                - p.loss_shares[i] * (s.T_zones[i] - T_outdoor)
                + p.air_exchange * exchanged
            ) / tau_zone_min + Q_K_per_min
        for i in range(p.n_zones):
            s.T_rads[i] += dT_rads[i] * dt_min
            s.T_zones[i] += dT_zones[i] * dt_min


def steady_state(
    params: MultiZonePlantParams, u_per_zone: list[float], T_outdoor: float
) -> MultiZonePlantState:
    """Solve the state the plant settles in under constant valves.

    Parameters
    ----------
    params : MultiZonePlantParams
        The plant.
    u_per_zone : list[float]
        Valve opening in [0, 1] per radiator.
    T_outdoor : float
        Outdoor temperature in °C.

    Returns
    -------
    MultiZonePlantState
        The settled zone and radiator temperatures.
    """
    n = params.n_zones
    matrix = np.zeros((2 * n, 2 * n))
    constants = np.zeros(2 * n)
    for i in range(n):
        gain = params.gain_heaters[i] * u_per_zone[i]
        coupling = params.coupling_rad_zone[i]
        matrix[i, i] = -(coupling + params.loss_shares[i]) - params.air_exchange * (
            n - 1
        )
        for j in range(n):
            if j != i:
                matrix[i, j] = params.air_exchange
        matrix[i, n + i] = coupling
        constants[i] = -params.loss_shares[i] * T_outdoor
        matrix[n + i, i] = 1.0
        matrix[n + i, n + i] = -(gain + 1.0)
        constants[n + i] = -gain * params.T_water
    solution = np.linalg.solve(matrix, constants)
    return MultiZonePlantState(
        T_zones=[float(value) for value in solution[:n]],
        T_rads=[float(value) for value in solution[n:]],
    )


def settled_state_at(
    params: MultiZonePlantParams,
    sensor_weights: tuple[float, ...],
    setpoint: float,
    T_outdoor: float,
) -> MultiZonePlantState:
    """Return the settled state whose room sensor reads ``setpoint``.

    Every radiator gets the same opening, found by bisection. A setpoint
    outside what the radiators reach settles at the nearest opening.

    Parameters
    ----------
    params : MultiZonePlantParams
        The plant.
    sensor_weights : tuple[float, ...]
        Share of each zone in the room sensor's reading.
    setpoint : float
        Temperature the room sensor is to read, in °C.
    T_outdoor : float
        Outdoor temperature in °C.

    Returns
    -------
    MultiZonePlantState
        The settled zone and radiator temperatures.
    """
    low, high = 0.0, 1.0
    for _ in range(50):
        opening = (low + high) / 2.0
        state = steady_state(params, [opening] * params.n_zones, T_outdoor)
        reading = sum(w * t for w, t in zip(sensor_weights, state.T_zones, strict=True))
        if reading < setpoint:
            low = opening
        else:
            high = opening
    return steady_state(params, [(low + high) / 2.0] * params.n_zones, T_outdoor)
