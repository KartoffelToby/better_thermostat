"""MPC v2 I/O contract — the dataclasses exchanged with the dispatcher.

Defines the per-cycle boundary types for the controller: :class:`MpcV2Input`
(sensor readings and gating flags fed in each cycle), :class:`MpcV2Output`
(the valve-percent recommendation handed back), and :class:`MpcV2Diagnostics`
(observer/identifier internals surfaced as entity attributes). These types are
deliberately HA-free so the controller core can be exercised in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from ..disturbance_forecast import HourlyForecastPoint
    from ..disturbance_profile import DisturbanceProfile


@dataclass
class MpcV2Input:
    """Inputs for one MPC v2 control cycle (v2's own I/O contract)."""

    key: str
    target_temperature: float
    room_temperature: float
    trv_temperature: float | None = None
    outdoor_temperature: float | None = None
    window_open: bool = False
    heating_allowed: bool = True
    max_opening_percent: float = 100.0
    # Last valve opening confirmed by the adapter or reported by the device.
    # It is deliberately the previous applied input, not this cycle's MPC
    # recommendation: writes can be deferred or fail after computation.
    applied_valve_percent: float | None = None
    # While set, the disturbance estimate and the error integral are held and
    # the controller plans on them; the observer keeps tracking the room.
    learning_paused: bool = False
    # Set while the room sensor has failed and ``room_temperature`` is the
    # mean of the TRVs' own readings. Their steps say nothing about the room
    # sensor's reporting threshold, so each such reading counts as fresh.
    room_temperature_from_trvs: bool = False
    bt_name: str | None = None
    entity_id: str | None = None
    # Learned gain expected at each future QP step, in K/min. When empty and
    # a profile is supplied, ``compute_mpc_v2`` builds it from the profile and
    # the hourly forecast on the controller's actual planning grid.
    predicted_disturbance_K_per_min: tuple[float, ...] = ()
    disturbance_profile: DisturbanceProfile | None = None
    hourly_forecast: tuple[HourlyForecastPoint, ...] = ()
    # Timezone-aware wall clock of this cycle. Only needed when the profile is
    # supplied and the prediction is built here; the pure core never reads the
    # system clock itself.
    now_wall: datetime | None = None


@dataclass
class MpcV2Diagnostics:
    """Per-cycle controller diagnostics, surfaced as entity attributes."""

    T_room_hat: float
    T_rad_hat: float
    D_hat_K_per_min: float
    tau_room_min: float
    coupling_rad_room: float
    normalised_innovation: float = 0.0
    disturbance_at_bound: bool = False
    unbounded_D_hat_K_per_min: float = 0.0
    # Learned gain the profile expects at the first step of this plan; None
    # while no predictive profile is active.
    predicted_disturbance_kelvin_per_min: float | None = None


@dataclass
class MpcV2Output:
    """Result of one MPC v2 control cycle."""

    valve_percent: int
    diagnostics: MpcV2Diagnostics
