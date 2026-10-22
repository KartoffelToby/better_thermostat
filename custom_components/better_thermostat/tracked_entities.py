"""The entities one entry's platforms have created and track for removal.

The sensor, number and switch platforms record the unique_ids of the
entities they create here, so that the entities of an algorithm, a preset or
a TRV the configuration dropped can be removed from the entity registry. One
record lives on each loaded entry's runtime data and goes with it on unload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .utils.calibration.pid import PidGain
    from .utils.const import CalibrationMode


@dataclass(frozen=True, slots=True)
class PresetNumberRef:
    """A tracked preset number entity: the preset whose temperature it sets.

    The heating and the cooling number of one preset both carry it.
    """

    preset: str


@dataclass(frozen=True, slots=True)
class PidNumberRef:
    """A tracked PID number entity: the TRV and the gain it sets."""

    trv: str
    gain: PidGain


class SwitchKind(StrEnum):
    """Which per-TRV switch a tracked switch entity is."""

    PID_AUTO_TUNE = "pid_auto_tune"
    CHILD_LOCK = "child_lock"
    RELEASE_TRV = "release_trv"


@dataclass(frozen=True, slots=True)
class SwitchRef:
    """A tracked switch entity: the TRV it belongs to and its kind."""

    trv: str
    kind: SwitchKind


@dataclass(slots=True)
class TrackedEntities:
    """The entities one entry has created, keyed by unique_id.

    Attributes
    ----------
    algorithm_sensors : dict[CalibrationMode, list[str]]
        The unique_ids of the diagnostic sensors of each calibration
        algorithm that has them. An algorithm whose sensors are all removed
        is dropped from it.
    preset_numbers : dict[str | None, PresetNumberRef]
        The preset number entities and the preset each one sets.
    pid_numbers : dict[str | None, PidNumberRef]
        The PID number entities and the TRV and gain each one sets.
    switches : dict[str | None, SwitchRef]
        The per-TRV switch entities and the TRV and kind of each one.
    """

    algorithm_sensors: dict[CalibrationMode, list[str]] = field(default_factory=dict)
    preset_numbers: dict[str | None, PresetNumberRef] = field(default_factory=dict)
    pid_numbers: dict[str | None, PidNumberRef] = field(default_factory=dict)
    switches: dict[str | None, SwitchRef] = field(default_factory=dict)
