"""MPC v2 I/O contract — the dataclasses exchanged with the dispatcher.

Defines the per-cycle boundary types for the controller: :class:`MpcV2Input`
(sensor readings and gating flags fed in each cycle), :class:`MpcV2Output`
(the valve-percent recommendation handed back), and :class:`MpcV2Diagnostics`
(observer/identifier internals surfaced as entity attributes). These types are
deliberately HA-free so the controller core can be exercised in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MpcV2Input:
    """Inputs for one MPC v2 control cycle (v2's own I/O contract)."""

    key: str
    target_temp_C: float | None
    current_temp_C: float | None
    trv_temp_C: float | None = None
    outdoor_temp_C: float | None = None
    window_open: bool = False
    heating_allowed: bool = True
    max_opening_pct: float | None = None
    bt_name: str | None = None
    entity_id: str | None = None


@dataclass
class MpcV2Diagnostics:
    """Per-cycle controller diagnostics, surfaced as entity attributes."""

    T_room_hat: float
    T_rad_hat: float
    D_hat_K_per_min: float
    tau_room_min: float
    coupling_rad_room: float


@dataclass
class MpcV2Output:
    """Result of one MPC v2 control cycle."""

    valve_percent: int
    diagnostics: MpcV2Diagnostics
