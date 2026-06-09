"""Immutable snapshot of the world as the control path sees it.

A :class:`WorldSnapshot` is the raw observation handed to the decision
kernel: target and measured temperatures, mode flags, environment, and
the reported state of every TRV. It is built once per control cycle by
the shell (``utils/snapshot.py``) and never mutated.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class HvacMode(StrEnum):
    """HVAC mode vocabulary of the core.

    Values match Home Assistant's ``HVACMode`` strings so that members
    compare equal to the shell's enum without importing it.
    """

    OFF = "off"
    HEAT = "heat"
    COOL = "cool"
    HEAT_COOL = "heat_cool"
    AUTO = "auto"


def parse_hvac_mode(value: str | None) -> HvacMode | None:
    """Map a raw mode string (or HA's string enum) onto the core vocabulary.

    Returns ``None`` for unknown or missing values.
    """
    if value is None:
        return None
    try:
        return HvacMode(str(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class TrvReported:
    """Reported state of a single TRV at snapshot time."""

    entity_id: str
    available: bool = True
    hvac_mode: HvacMode | None = None
    current_temp: float | None = None
    setpoint: float | None = None
    min_temp: float | None = None
    max_temp: float | None = None
    valve_max_opening: float | None = None


@dataclass(frozen=True)
class WorldSnapshot:
    """Complete, immutable observation of one control cycle."""

    now: datetime
    now_monotonic: float
    target_temp: float | None = None
    target_cooltemp: float | None = None
    hvac_mode: HvacMode | None = None
    room_temp: float | None = None
    room_temp_filtered: float | None = None
    temp_slope: float | None = None
    window_open: bool | None = None
    call_for_heat: bool = True
    preset_mode: str | None = None
    tolerance: float = 0.0
    outdoor_temp: float | None = None
    is_day: bool = True
    solar_intensity: float = 0.0
    startup_running: bool = False
    in_maintenance: bool = False
    ignore_states: bool = False
    degraded: bool = False
    min_temp: float | None = None
    max_temp: float | None = None
    trvs: Mapping[str, TrvReported] = field(default_factory=dict)
