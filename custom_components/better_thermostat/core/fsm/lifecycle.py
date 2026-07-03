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
    """Move INITIALISING to STARTING; later phases are unchanged.

    Parameters
    ----------
    state : LifecycleState
        Current lifecycle state.
    grace_until : datetime | None, optional
        Deadline until which degraded-mode annunciation stays suppressed.

    Returns
    -------
    LifecycleState
        STARTING state when previously INITIALISING, otherwise ``state``.
    """
    if state.phase != LifecyclePhase.INITIALISING:
        return state
    return LifecycleState(phase=LifecyclePhase.STARTING, grace_until=grace_until)


def extend_grace(state: LifecycleState, grace_until: datetime) -> LifecycleState:
    """Set the annunciation grace deadline while STARTING.

    Parameters
    ----------
    state : LifecycleState
        Current lifecycle state.
    grace_until : datetime
        New grace deadline.

    Returns
    -------
    LifecycleState
        Updated state while STARTING, otherwise ``state`` unchanged.
    """
    if state.phase != LifecyclePhase.STARTING:
        return state
    return LifecycleState(phase=LifecyclePhase.STARTING, grace_until=grace_until)


def tick(state: LifecycleState, now: datetime) -> LifecycleState:
    """Promote STARTING to RUNNING once the grace window has passed.

    Parameters
    ----------
    state : LifecycleState
        Current lifecycle state.
    now : datetime
        Current time used to evaluate the grace window.

    Returns
    -------
    LifecycleState
        RUNNING state once the grace window elapsed, otherwise ``state``.
    """
    if state.phase == LifecyclePhase.STARTING and not state.in_grace(now):
        return LifecycleState(phase=LifecyclePhase.RUNNING)
    return state


def stop(state: LifecycleState) -> LifecycleState:
    """Move any phase to STOPPING (terminal).

    Parameters
    ----------
    state : LifecycleState
        Current lifecycle state.

    Returns
    -------
    LifecycleState
        The terminal STOPPING state.
    """
    return LifecycleState(phase=LifecyclePhase.STOPPING)
