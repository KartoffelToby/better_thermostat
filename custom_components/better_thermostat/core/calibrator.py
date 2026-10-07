"""Calibrator strategy contract and capability model.

A calibrator turns observations into valve intent. The key split is
``observe`` (always runs, even in standby, so the model keeps
converging) versus ``actuate`` (only when the control mode allows it
and the calibrator is ready) — the precondition for bumpless transfer
when a degraded mode hands control back.

Capabilities are strictly nested: ``ready`` implies ``healthy`` implies
``configured``. A cold start needs only ``healthy``; ``ready`` guards
the re-promotion after a gap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .snapshot import WorldSnapshot


class CalibratorHealth(StrEnum):
    """Severity grades a calibrator can self-report."""

    HEALTHY = "healthy"
    NON_FINITE = "non_finite"
    OSCILLATING = "oscillating"
    RUNAWAY_GAINS = "runaway_gains"
    WINDUP_SUSPECT = "windup_suspect"


@dataclass(frozen=True)
class Capability:
    """Nested capability levels of a calibrator.

    Construction enforces the nesting invariant, so an inconsistent
    combination cannot exist.
    """

    configured: bool = False
    healthy: bool = False
    ready: bool = False

    def __post_init__(self) -> None:
        """Enforce ready ⊆ healthy ⊆ configured."""
        if self.ready and not self.healthy:
            raise ValueError("ready requires healthy")
        if self.healthy and not self.configured:
            raise ValueError("healthy requires configured")


@runtime_checkable
class Calibrator(Protocol):
    """Strategy contract for calibration controllers.

    Exactly three methods: ``observe`` runs every cycle (also in
    standby), ``is_ready`` gates actuation, ``actuate`` emits the valve
    percentage. Annunciation lives in the separate
    :class:`AnnunciatingCalibrator` extension so the control contract
    stays minimal.
    """

    def observe(self, snapshot: WorldSnapshot, now: float) -> None:
        """Feed one observation into the model (always, even in standby)."""
        ...

    def is_ready(self) -> bool:
        """Whether the model has converged enough to actuate."""
        ...

    def actuate(self, snapshot: WorldSnapshot) -> float | None:
        """Return the valve percentage to command, or None for no intent."""
        ...


@runtime_checkable
class AnnunciatingCalibrator(Calibrator, Protocol):
    """Calibrator that additionally self-reports capability and health."""

    def capability(self) -> Capability:
        """Report the current capability level."""
        ...

    def health(self) -> CalibratorHealth:
        """Report the current health grade."""
        ...


# Oscillation detection: annunciation only. A detector that
# automatically backs gains off can thrash a controller on a false
# positive — worse than the oscillation it reacts to — so the backoff
# stays a manual decision until the detector is validated against the
# calibration benchmark.
OSCILLATION_WINDOW = 10
OSCILLATION_MIN_REVERSALS = 4
OSCILLATION_MIN_SWING_PCT = 20.0


def detect_oscillation(outputs: Sequence[float]) -> bool:
    """Whether a command history shows sustained output oscillation.

    Looks at the last :data:`OSCILLATION_WINDOW` commanded percentages
    and reports True when at least :data:`OSCILLATION_MIN_REVERSALS`
    direction reversals occur between swings of at least
    :data:`OSCILLATION_MIN_SWING_PCT` points each.
    """
    recent = list(outputs)[-OSCILLATION_WINDOW:]
    deltas = [later - earlier for earlier, later in zip(recent, recent[1:])]
    significant = [d for d in deltas if abs(d) >= OSCILLATION_MIN_SWING_PCT]
    reversals = sum(
        1
        for first, second in zip(significant, significant[1:])
        if (first > 0) != (second > 0)
    )
    return reversals >= OSCILLATION_MIN_REVERSALS
