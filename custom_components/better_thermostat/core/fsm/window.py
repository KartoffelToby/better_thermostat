"""Window region: closed -> opening -> open -> closing -> closed.

The sensor's raw reading is debounced in both directions: a change only
commits after it has persisted for the configured delay. While a change
is pending, the *committed* phase keeps ruling the control law.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WindowPhase(StrEnum):
    """Discrete phases of the window region."""

    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"


@dataclass(frozen=True)
class WindowState:
    """State of the window region.

    ``pending_since`` carries the monotonic timestamp at which the
    currently pending transition (OPENING/CLOSING) was observed.
    """

    phase: WindowPhase = WindowPhase.CLOSED
    pending_since: float | None = None

    @property
    def effective_open(self) -> bool:
        """Whether the control law should treat the window as open."""
        return self.phase in (WindowPhase.OPEN, WindowPhase.CLOSING)


@dataclass(frozen=True)
class WindowParams:
    """Debounce delays in seconds (open and close direction)."""

    open_delay_s: float = 0.0
    close_delay_s: float = 0.0


def step(
    state: WindowState, sensor_open: bool, now: float, params: WindowParams
) -> WindowState:
    """Advance the window region by one observation.

    ``sensor_open`` is the raw sensor reading, ``now`` the monotonic time
    of the observation. The caller re-steps after the configured delay to
    let a pending transition commit.
    """
    phase = state.phase

    if phase == WindowPhase.CLOSED:
        if sensor_open:
            state = WindowState(phase=WindowPhase.OPENING, pending_since=now)
            return _commit_if_due(state, now, params)
        return state

    if phase == WindowPhase.OPENING:
        if not sensor_open:
            # False positive: the window closed again before the delay ran out.
            return WindowState(phase=WindowPhase.CLOSED)
        return _commit_if_due(state, now, params)

    if phase == WindowPhase.OPEN:
        if not sensor_open:
            state = WindowState(phase=WindowPhase.CLOSING, pending_since=now)
            return _commit_if_due(state, now, params)
        return state

    # CLOSING
    if sensor_open:
        # False positive: the window reopened before the delay ran out.
        return WindowState(phase=WindowPhase.OPEN)
    return _commit_if_due(state, now, params)


def _commit_if_due(state: WindowState, now: float, params: WindowParams) -> WindowState:
    """Commit a pending transition once its delay has elapsed."""
    if state.phase == WindowPhase.OPENING and state.pending_since is not None:
        if now - state.pending_since >= params.open_delay_s:
            return WindowState(phase=WindowPhase.OPEN)
    if state.phase == WindowPhase.CLOSING and state.pending_since is not None:
        if now - state.pending_since >= params.close_delay_s:
            return WindowState(phase=WindowPhase.CLOSED)
    return state
