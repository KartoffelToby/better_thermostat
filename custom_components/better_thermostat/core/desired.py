"""Desired state emitted by the decision kernel.

A :class:`DesiredState` expresses intent, not commands: what each TRV
should be doing right now. The shell translates it into device writes
(adapters), so the kernel never performs IO itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .snapshot import HvacMode


class Suppression(StrEnum):
    """Why heating is suppressed although the mode allows it.

    Carried on an OFF intent so the shell can apply it (a literal OFF)
    without re-deriving the cause from the kernel's regions.
    """

    WINDOW = "window"
    NO_CALL_FOR_HEAT = "no_call_for_heat"


@dataclass(frozen=True)
class TrvDesired:
    """Intent for a single TRV."""

    entity_id: str
    hvac_mode: HvacMode | None = None
    setpoint: float | None = None
    valve_percent: float | None = None
    offset: float | None = None
    suppression: Suppression | None = None


@dataclass(frozen=True)
class DesiredState:
    """Complete intent of one control cycle."""

    call_for_heat: bool = False
    trvs: Mapping[str, TrvDesired] = field(default_factory=dict)
