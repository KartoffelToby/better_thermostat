"""Desired state emitted by the decision kernel.

A :class:`DesiredState` expresses intent, not commands: what each TRV
should be doing right now. The shell translates it into device writes
(adapters), so the kernel never performs IO itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from .snapshot import HvacMode


class Suppression(StrEnum):
    """Why heating is suppressed although the mode allows it.

    Carried on an OFF intent so the shell can apply it (a literal OFF)
    without re-deriving the cause from the kernel's regions.
    """

    WINDOW = "window"
    DOOR = "door"
    NO_CALL_FOR_HEAT = "no_call_for_heat"


class HeatingBlock(StrEnum):
    """Which tier of the cascade keeps the room from being heated.

    One value per tier above the heating rung. The thermostat publishes the
    value of its latest decision as the ``heating_blocked_by`` attribute, so
    the values are a public contract: automations compare against them.
    """

    DEVICES_RELEASED = "devices_released"
    STARTUP = "startup"
    VALVE_MAINTENANCE = "valve_maintenance"
    HVAC_OFF = "hvac_off"
    WINDOW = "window"
    DOOR = "door"
    NO_CALL_FOR_HEAT = "no_call_for_heat"


# The block each suppression on a TRV intent stands for. A suppression and
# its block share one value, so the attribute reads the same word the flight
# recorder stores on the intents.
SUPPRESSION_BLOCKS: Final[Mapping[Suppression, HeatingBlock]] = MappingProxyType(
    {
        Suppression.WINDOW: HeatingBlock.WINDOW,
        Suppression.DOOR: HeatingBlock.DOOR,
        Suppression.NO_CALL_FOR_HEAT: HeatingBlock.NO_CALL_FOR_HEAT,
    }
)


@dataclass(frozen=True)
class TrvDesired:
    """Intent for a single TRV."""

    entity_id: str
    hvac_mode: HvacMode | None = None
    setpoint: float | None = None
    valve_percent: float | None = None
    calibration_offset: float | None = None
    suppression: Suppression | None = None


@dataclass(frozen=True)
class DesiredState:
    """Complete intent of one control cycle.

    ``devices_released`` says the room's devices are the user's for this
    cycle: the shell commands none of them, the cooler included, and the
    cycle teaches the learners nothing. TRVs released one by one leave it
    False; they receive no intent, and once all of them are released the
    block below says so.

    ``heating_blocked_by`` names the tier that keeps the room from being
    heated, and is None while the heating rung rules.
    """

    call_for_heat: bool = False
    trvs: Mapping[str, TrvDesired] = field(default_factory=dict)
    devices_released: bool = False
    heating_blocked_by: HeatingBlock | None = None
