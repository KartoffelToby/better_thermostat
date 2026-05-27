"""Lumped-RC thermal plant simulator (RC2 / RC3 + optional pipe transport delay).

RC2 mode (``tau_wall_min == 0``):

    dT_rad/dt  = (1 / tau_rad_min) * (
        gain_heater * u * (T_water_C - T_rad)
        - (T_rad - T_room)
    )
    dT_room/dt = (1 / tau_room_min) * (
        coupling_rad_room * (T_rad - T_room)
        - (T_room - T_outdoor)
    ) + Q_K_per_min

RC3 mode (``tau_wall_min > 0``):

    dT_rad/dt  = (1 / tau_rad_min) * (
        gain_heater * u * (T_water_C - T_rad)
        - (T_rad - T_room)
    )
    dT_room/dt = (1 / tau_room_min) * (
        coupling_rad_room * (T_rad - T_room)
        - r_room_wall * (T_room - T_wall)
    ) + Q_K_per_min
    dT_wall/dt = (1 / tau_wall_min) * (
        r_room_wall * (T_room - T_wall)
        - (T_wall - T_outdoor)
    )

The RC3 mode introduces a slow wall thermal mass between the room and the
outdoor. Heat now flows ``Room → Wall → Outdoor`` instead of directly to
outdoor, giving the room a long-tail "thermal coast" after the heater
stops — closer to real residential buildings (Bacher & Madsen 2011,
ISO 52016-1).

Pipe-transport delay: when ``valve_command_delay_s > 0`` the radiator
does not see the *current* commanded ``u`` but the command from
``valve_command_delay_s`` ago. Models the boiler → radiator transport
lag (typically 30 s – 3 min; Burford & Madsen 2019).

Integration uses explicit Euler with a step size that the caller chooses.
For tau_rad_min >= 5 min and step sizes <= 60 s, this is comfortably stable
in all modes.

u is in [0, 1]; Q_K_per_min is an external disturbance heat-rate (K/min)
that already includes the room thermal capacity.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .sensor import SensorParams


@dataclass
class PlantParams:
    """Thermal parameters of a lumped-RC plant.

    With ``tau_wall_min <= 0`` the plant is RC2 (room + radiator). Setting
    ``tau_wall_min > 0`` enables the RC3 wall layer.
    """

    tau_room_min: float = 240.0
    tau_rad_min: float = 15.0
    gain_heater: float = 0.5
    coupling_rad_room: float = 1.0
    T_water_C: float = 65.0
    # RC3 wall layer (opt-in via ``tau_wall_min > 0``).
    tau_wall_min: float = 0.0
    r_room_wall: float = 1.0
    # Pipe transport delay (seconds) between commanded u and the u seen
    # at the radiator. ``0`` disables the delay.
    valve_command_delay_s: float = 0.0


@dataclass
class PlantState:
    """Mutable simulator state.

    ``T_wall_C`` is only meaningful in RC3 mode; in RC2 mode it stays at
    whatever value it was initialised to. The plant initialises it to the
    room temperature if the caller leaves it ``None``.
    """

    T_room_C: float
    T_rad_C: float
    T_wall_C: float | None = None


# --- RC2 profiles ---

PROFILE_FAST_SMALL = PlantParams(
    tau_room_min=120.0,
    tau_rad_min=8.0,
    gain_heater=1.5,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)

PROFILE_STANDARD = PlantParams(
    tau_room_min=480.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)

PROFILE_LARGE_SLOW = PlantParams(
    tau_room_min=720.0,
    tau_rad_min=25.0,
    gain_heater=2.5,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)

PROFILE_UNDERFLOOR = PlantParams(
    tau_room_min=480.0,
    tau_rad_min=60.0,
    gain_heater=2.5,
    coupling_rad_room=0.8,
    T_water_C=45.0,
)

# Representative-residential profiles derived from cooling-window fits to
# Home Assistant recorder data; see ``plant_fit/fit_plant.py`` for the
# methodology and ``plant_fit/generate_synthetic_data.py`` for an
# end-to-end synthetic equivalent.
PROFILE_REAL_WOHNZIMMER = PlantParams(
    tau_room_min=570.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)

PROFILE_REAL_KUCHE = PlantParams(
    tau_room_min=1011.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)


# RC3 profiles split the lumped room time constant across the room-air
# component (τ_room) and a wall mass (τ_wall). Air takes ≈ 1/8 of the
# lumped τ, wall ≈ 7/8 — consistent with Bacher & Madsen 2011 and the
# Modelica Buildings library ReducedOrder.RC reference.


PROFILE_STANDARD_RC3 = PlantParams(
    tau_room_min=60.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
    tau_wall_min=900.0,
    r_room_wall=1.0,
)

PROFILE_REAL_WOHNZIMMER_RC3 = PlantParams(
    tau_room_min=70.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
    tau_wall_min=1100.0,
    r_room_wall=1.0,
)


# Realistic profile: RC3 wall mass, equal-percentage-friendly gains, and
# a moderate pipe transport delay. To use the equal-percentage actuator
# with this plant, pass
# ``actuator_params=ActuatorParams(profile=EQUAL_PERCENTAGE)`` on the
# scenario.

# Heat-pump / low-temperature setup: supply ~42 °C instead of 65 °C.
# Same loss and coupling parameters as STANDARD; only the driving water
# temperature is reduced, which halves the effective heating-power gain
# at full valve opening.
PROFILE_BOILER_LIMITED = PlantParams(
    tau_room_min=480.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=42.0,
)


# DOE Reference Building–derived envelope classes.
#
# Lumped-parameter approximations of the three residential envelope
# generations used by the US DOE Building Energy Codes Program prototype
# buildings (energycodes.gov/prototype-building-models, IECC editions
# 1980 / 2004-2009 / 2012-2021). Per-room values, not whole-building:
# ~20 m² living room, ~50 m³ air volume, single external wall.
#
# Method (per generation):
# * Wall + window + infiltration UA -> overall heat-loss coefficient.
# * Effective room thermal capacity ~3× the dry-air mass, lumping
#   furniture and partition walls into the air node (RC2) or splitting
#   the slow mass into a wall node (RC3, tau_wall ≈ 5-10× tau_room).
# * gain_heater scaled so a typical radiator delivers steady-state heat
#   at u ≈ 0.3-0.5 with the envelope's design heat load at -5 °C outdoor.
#
# These profiles complement the existing PROFILE_STANDARD / _REAL_*
# fits (which were tuned against measured BT installations) with
# explicit DOE-codified envelope classes for cross-validation.

PROFILE_DOE_SFD_PRE1980 = PlantParams(
    # Single-family detached, uninsulated brick/wood-frame (pre-1980
    # building stock, dominant in older European housing). High UA,
    # low effective capacity -> short tau.
    tau_room_min=240.0,
    tau_rad_min=20.0,
    gain_heater=2.5,
    coupling_rad_room=1.0,
    T_water_C=70.0,
)

PROFILE_DOE_SFD_2004 = PlantParams(
    # Single-family detached at 2004-era IECC / EnEV 2002 envelope.
    # Wall U ≈ 0.35 W/m²K, window U ≈ 1.6 W/m²K, ACH ≈ 0.5.
    tau_room_min=480.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
)

PROFILE_DOE_SFD_2010 = PlantParams(
    # Energy-efficient single-family at 2012+ IECC / EnEV 2009 envelope.
    # Wall U ≈ 0.20 W/m²K, triple-pane windows, ACH ≈ 0.3, low-temp heating.
    tau_room_min=720.0,
    tau_rad_min=15.0,
    gain_heater=1.8,
    coupling_rad_room=1.0,
    T_water_C=55.0,
)

PROFILE_DOE_MIDRISE_APT = PlantParams(
    # Mid-rise apartment unit, 2010+ envelope. Adjacent units act as
    # thermal buffers -> moderate tau but mild outdoor-coupling.
    tau_room_min=600.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=60.0,
)


# Reverse-acting "cooling" plant — same RC structure, but the radiator
# circuit carries chilled water (≈ 15 °C). At u > 0 the radiator cools
# the room instead of heating it. Used by S26 to expose how the BT/PID
# controllers behave when the system's sign is flipped relative to their
# design assumption (heating only).
PROFILE_COOLING = PlantParams(
    tau_room_min=480.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=15.0,
)


PROFILE_REALISTIC = PlantParams(
    tau_room_min=70.0,
    tau_rad_min=15.0,
    gain_heater=2.0,
    coupling_rad_room=1.0,
    T_water_C=65.0,
    tau_wall_min=1100.0,
    r_room_wall=1.0,
    valve_command_delay_s=60.0,  # 1 min boiler→radiator transport
)


# Realistic sensor settings to combine with PROFILE_REALISTIC. Not used
# automatically by any scenario; opt-in via ``scenario.sensor_params``.

REALISTIC_SENSOR_PARAMS = SensorParams(
    sample_interval_s=60.0,
    ema_alpha=0.7,
    noise_std_K=0.05,
    thermal_lag_s=90.0,  # 1.5 min sensor thermal mass
)


class TwoStatePlant:
    """Lumped-RC thermal plant.

    Two internal states by default, three when the RC3 wall layer is
    enabled via ``params.tau_wall_min > 0``. The plant owns its mutable
    state. The caller drives it forward in time via :meth:`step`.
    """

    def __init__(self, params: PlantParams, initial: PlantState) -> None:
        self.params = params
        # Default T_wall to T_room when not provided. Important for RC3.
        wall = initial.T_wall_C if initial.T_wall_C is not None else initial.T_room_C
        self.state = PlantState(
            T_room_C=initial.T_room_C, T_rad_C=initial.T_rad_C, T_wall_C=wall
        )
        # Pipe-delay buffer is allocated lazily on the first step() call,
        # once we know the simulator's step size.
        self._u_delay_buffer: deque[float] = deque()
        self._u_buffer_target_len: int = 0

    def step(
        self, dt_s: float, u: float, T_outdoor_C: float, Q_K_per_min: float = 0.0
    ) -> PlantState:
        """Advance the plant by ``dt_s`` seconds. Returns the new state."""
        if dt_s <= 0.0:
            return self.state

        dt_min = dt_s / 60.0
        p = self.params
        s = self.state
        u_clamped = max(0.0, min(1.0, u))

        # Apply pipe-transport delay: the radiator sees u from a few seconds
        # ago, not the just-commanded value.
        if p.valve_command_delay_s > 0.0:
            if self._u_buffer_target_len == 0:
                self._u_buffer_target_len = max(
                    1, round(p.valve_command_delay_s / dt_s)
                )
                # Prime the buffer with the current u so the first
                # ``delay`` steps are not artificially zero.
                self._u_delay_buffer.extend([u_clamped] * self._u_buffer_target_len)
            self._u_delay_buffer.append(u_clamped)
            u_clamped = self._u_delay_buffer.popleft()

        # Radiator dynamics — identical in RC2 and RC3.
        dT_rad = (
            p.gain_heater * u_clamped * (p.T_water_C - s.T_rad_C)
            - (s.T_rad_C - s.T_room_C)
        ) / p.tau_rad_min

        if p.tau_wall_min > 0.0:
            # RC3 dynamics: room exchanges with the wall instead of outdoor.
            wall = s.T_wall_C if s.T_wall_C is not None else s.T_room_C

            dT_room = (
                p.coupling_rad_room * (s.T_rad_C - s.T_room_C)
                - p.r_room_wall * (s.T_room_C - wall)
            ) / p.tau_room_min

            dT_wall = (
                p.r_room_wall * (s.T_room_C - wall) - (wall - T_outdoor_C)
            ) / p.tau_wall_min

            s.T_rad_C += dT_rad * dt_min
            s.T_room_C += (dT_room + Q_K_per_min) * dt_min
            s.T_wall_C = wall + dT_wall * dt_min
        else:
            # RC2 dynamics: room exchanges directly with outdoor.
            dT_room = (
                p.coupling_rad_room * (s.T_rad_C - s.T_room_C)
                - (s.T_room_C - T_outdoor_C)
            ) / p.tau_room_min

            s.T_rad_C += dT_rad * dt_min
            s.T_room_C += (dT_room + Q_K_per_min) * dt_min

        return s
