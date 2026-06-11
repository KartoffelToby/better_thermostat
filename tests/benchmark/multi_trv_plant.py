"""Multi-TRV plant model.

N parallel radiators feed one room. Each radiator has its own valve
position, gain, room-coupling and optional sensor offset (so that BT's
``distribute_valve_percent`` heuristic can pick up asymmetry).

The room equation is identical to :class:`TwoStatePlant` — heat in from
all radiators, heat out to outdoor, plus an optional disturbance term.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MultiTrvPlantParams:
    """Thermal parameters for N radiators feeding one room."""

    n_trvs: int = 3
    tau_room_min: float = 480.0
    tau_rad_min: float = 15.0
    gain_heaters: list[float] = field(default_factory=lambda: [2.0, 2.0, 2.0])
    coupling_rad_room: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    T_water_C: float = 65.0
    # Position-induced offsets applied to the *reported* TRV-internal
    # temperatures. Lets BT's distribute_valve_percent see asymmetry even
    # when the radiator-state values are otherwise close.
    trv_sensor_offsets_K: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Per-TRV deadband: commands strictly below this percent are zeroed
    # for that TRV. Empty list = no deadband. Models heterogeneous
    # hardware sharing one room (e.g. one Sonoff TRVZB pre-FW 1.3 with a
    # 22 % deadband mixed with two normal TRVs).
    deadband_pcts_per_trv: list[float] = field(default_factory=list)


@dataclass
class MultiTrvPlantState:
    """Mutable simulator state for a multi-TRV plant."""

    T_room_C: float
    T_rads_C: list[float]


class MultiTrvPlant:
    """Two-state-style plant generalised to N parallel radiators."""

    def __init__(
        self, params: MultiTrvPlantParams, initial: MultiTrvPlantState
    ) -> None:
        if len(initial.T_rads_C) != params.n_trvs:
            raise ValueError(
                f"initial.T_rads_C has {len(initial.T_rads_C)} entries, "
                f"params.n_trvs is {params.n_trvs}"
            )
        if len(params.gain_heaters) != params.n_trvs:
            raise ValueError("gain_heaters length must equal n_trvs")
        if len(params.coupling_rad_room) != params.n_trvs:
            raise ValueError("coupling_rad_room length must equal n_trvs")
        if len(params.trv_sensor_offsets_K) != params.n_trvs:
            raise ValueError("trv_sensor_offsets_K length must equal n_trvs")
        if params.deadband_pcts_per_trv and (
            len(params.deadband_pcts_per_trv) != params.n_trvs
        ):
            raise ValueError(
                "deadband_pcts_per_trv must be empty or have length n_trvs"
            )
        self.params = params
        self.state = MultiTrvPlantState(
            T_room_C=initial.T_room_C, T_rads_C=list(initial.T_rads_C)
        )

    def step(
        self,
        dt_s: float,
        u_per_trv: list[float],
        T_outdoor_C: float,
        Q_K_per_min: float = 0.0,
    ) -> MultiTrvPlantState:
        """Advance the plant by ``dt_s`` seconds. ``u_per_trv`` is one value in [0,1] per radiator."""
        if dt_s <= 0.0:
            return self.state
        p = self.params
        s = self.state
        if len(u_per_trv) != p.n_trvs:
            raise ValueError(
                f"u_per_trv has {len(u_per_trv)} entries, expected {p.n_trvs}"
            )

        dt_min = dt_s / 60.0
        dT_rads: list[float] = []
        Q_to_room = 0.0
        for i in range(p.n_trvs):
            u_i = max(0.0, min(1.0, u_per_trv[i]))
            if p.deadband_pcts_per_trv and (u_i * 100.0) < p.deadband_pcts_per_trv[i]:
                u_i = 0.0
            heat_in = p.gain_heaters[i] * u_i * (p.T_water_C - s.T_rads_C[i])
            heat_out = s.T_rads_C[i] - s.T_room_C
            dT_rad = (heat_in - heat_out) / p.tau_rad_min
            dT_rads.append(dT_rad)
            Q_to_room += p.coupling_rad_room[i] * (s.T_rads_C[i] - s.T_room_C)

        dT_room = (Q_to_room - (s.T_room_C - T_outdoor_C)) / p.tau_room_min

        for i in range(p.n_trvs):
            s.T_rads_C[i] += dT_rads[i] * dt_min
        s.T_room_C += (dT_room + Q_K_per_min) * dt_min
        return s

    def reported_trv_temps(self) -> list[float]:
        """Return the per-TRV temperatures as the controller's distribute logic sees them."""
        return [
            self.state.T_rads_C[i] + self.params.trv_sensor_offsets_K[i]
            for i in range(self.params.n_trvs)
        ]


# --- Pre-tuned multi-TRV profiles ---


PROFILE_MULTI_SYMMETRIC = MultiTrvPlantParams(
    n_trvs=3,
    tau_room_min=480.0,
    tau_rad_min=15.0,
    gain_heaters=[2.0, 2.0, 2.0],
    coupling_rad_room=[1.0, 1.0, 1.0],
    T_water_C=65.0,
    trv_sensor_offsets_K=[0.0, 0.0, 0.0],
)

PROFILE_MULTI_ASYMMETRIC = MultiTrvPlantParams(
    n_trvs=3,
    tau_room_min=480.0,
    tau_rad_min=15.0,
    # One smaller radiator in a cold corner, one normal, one bigger near the
    # warmest area.
    gain_heaters=[1.5, 2.0, 2.5],
    coupling_rad_room=[0.8, 1.0, 1.2],
    T_water_C=65.0,
    # Cold corner reads -1.5 K below room, near-window reads +0.5 K.
    trv_sensor_offsets_K=[-1.5, 0.0, 0.5],
)


# Heterogeneous hardware: same RC parameters as SYMMETRIC, but the
# middle TRV is a Sonoff TRVZB pre-FW 1.3 with a 22 % internal deadband
# — small valve commands are completely ignored by that one TRV.
PROFILE_MULTI_HETEROGENEOUS = MultiTrvPlantParams(
    n_trvs=3,
    tau_room_min=480.0,
    tau_rad_min=15.0,
    gain_heaters=[2.0, 2.0, 2.0],
    coupling_rad_room=[1.0, 1.0, 1.0],
    T_water_C=65.0,
    trv_sensor_offsets_K=[0.0, 0.0, 0.0],
    deadband_pcts_per_trv=[2.0, 22.0, 2.0],
)
