"""Lifecycle region: INITIALISING -> STARTING(grace) -> RUNNING -> STOPPING.

INITIALISING covers the startup sequence (no control happens), STARTING
is the post-startup grace window during which degraded-mode annunciation
stays quiet, RUNNING is normal operation, and STOPPING marks removal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class LifecyclePhase(StrEnum):
    """Discrete phases of the lifecycle region."""

    INITIALISING = "initialising"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass(frozen=True)
class LifecycleState:
    """State of the lifecycle region."""

    phase: LifecyclePhase = LifecyclePhase.INITIALISING
    grace_until: datetime | None = None

    @property
    def startup_running(self) -> bool:
        """Whether the startup sequence is still in progress."""
        return self.phase == LifecyclePhase.INITIALISING

    def in_grace(self, now: datetime) -> bool:
        """Whether degraded-mode annunciation is still suppressed.

        Mirrors the shell semantics: grace only counts once a deadline
        has been set after startup.
        """
        return self.grace_until is not None and now < self.grace_until


def startup_finished(
    state: LifecycleState, grace_until: datetime | None = None
) -> LifecycleState:
    """Move INITIALISING to STARTING; later phases are unchanged."""
    if state.phase != LifecyclePhase.INITIALISING:
        return state
    return LifecycleState(phase=LifecyclePhase.STARTING, grace_until=grace_until)


def extend_grace(state: LifecycleState, grace_until: datetime) -> LifecycleState:
    """Set the annunciation grace deadline while STARTING."""
    if state.phase != LifecyclePhase.STARTING:
        return state
    return LifecycleState(phase=LifecyclePhase.STARTING, grace_until=grace_until)


def tick(state: LifecycleState, now: datetime) -> LifecycleState:
    """Promote STARTING to RUNNING once the grace window has passed."""
    if state.phase == LifecyclePhase.STARTING and not state.in_grace(now):
        return LifecycleState(phase=LifecyclePhase.RUNNING)
    return state


def stop(state: LifecycleState) -> LifecycleState:
    """Move any phase to STOPPING (terminal)."""
    return LifecycleState(phase=LifecyclePhase.STOPPING)
