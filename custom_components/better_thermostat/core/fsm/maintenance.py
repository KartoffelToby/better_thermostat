"""Maintenance region: idle -> due -> running -> idle (rescheduled).

The region owns the schedule (``next_due``) and the run guard. The
invariant that motivated it: a maintenance run must never be able to
block control permanently — ``is_blocking`` stops honoring a RUNNING
phase once it exceeds the configured maximum runtime, and finishing a
run always reschedules and returns to IDLE.

Each phase is its own class, so only a RUNNING state carries a start
timestamp and no state can be RUNNING without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# A valve exercise takes a few minutes per TRV; an hour means it died.
MAX_RUN_S = 3600.0


@dataclass(frozen=True)
class MaintenanceIdle:
    """Maintenance region waiting for ``next_due``."""

    next_due: datetime | None = None

    def is_blocking(
        self, now_monotonic: float, max_run_seconds: float = MAX_RUN_S
    ) -> bool:
        """Whether this region currently pre-empts control; never while idle."""
        return False


@dataclass(frozen=True)
class MaintenanceDue:
    """Maintenance region with a run armed but not yet started."""

    next_due: datetime | None = None

    def is_blocking(
        self, now_monotonic: float, max_run_seconds: float = MAX_RUN_S
    ) -> bool:
        """Whether this region currently pre-empts control; never while armed."""
        return False


@dataclass(frozen=True)
class MaintenanceRunning:
    """Maintenance region with a valve exercise in progress.

    ``running_since`` is the monotonic start of the run, the age that
    ``is_blocking`` measures against the maximum runtime.
    """

    running_since: float
    next_due: datetime | None = None

    def is_blocking(
        self, now_monotonic: float, max_run_seconds: float = MAX_RUN_S
    ) -> bool:
        """Whether this region currently pre-empts control.

        A run older than ``max_run_seconds`` is treated as dead and stops
        blocking, bounding how long maintenance can pre-empt control.
        """
        return (now_monotonic - self.running_since) < max_run_seconds


type MaintenanceState = MaintenanceIdle | MaintenanceDue | MaintenanceRunning


def evaluate_tick(
    state: MaintenanceState, now: datetime, *, window_open: bool, has_enabled_trvs: bool
) -> MaintenanceState:
    """Advance the region on a scheduler tick.

    An open window pushes the schedule out an hour, which terminates
    because a window closes again. Without any maintenance-enabled TRV
    the next check moves a week out. Otherwise a due schedule arms DUE.

    HVAC mode is deliberately not a postpone reason. A valve held shut
    across a heating-off summer is the one that seizes, so an off
    thermostat is when the exercise matters most.

    Parameters
    ----------
    state : MaintenanceState
        Current maintenance state.
    now : datetime
        Current time used to evaluate the schedule.
    window_open : bool
        Whether a window is currently open.
    has_enabled_trvs : bool
        Whether any TRV has valve maintenance enabled.

    Returns
    -------
    MaintenanceState
        The advanced state (possibly DUE or with a postponed schedule).
    """
    if not isinstance(state, MaintenanceIdle):
        return state
    if state.next_due is not None and now < state.next_due:
        return state
    if window_open:
        return MaintenanceIdle(next_due=now + timedelta(hours=1))
    if not has_enabled_trvs:
        return MaintenanceIdle(next_due=now + timedelta(days=7))
    return MaintenanceDue(next_due=state.next_due)


def start_run(state: MaintenanceState, now_monotonic: float) -> MaintenanceState:
    """Move DUE to RUNNING; any other phase is unchanged (no double starts).

    Parameters
    ----------
    state : MaintenanceState
        Current maintenance state.
    now_monotonic : float
        Monotonic timestamp marking the start of the run.

    Returns
    -------
    MaintenanceState
        RUNNING state when previously DUE, otherwise ``state`` unchanged.
    """
    if not isinstance(state, MaintenanceDue):
        return state
    return MaintenanceRunning(running_since=now_monotonic, next_due=state.next_due)


def finish_run(state: MaintenanceState, next_due: datetime | None) -> MaintenanceIdle:
    """Return the region to IDLE with the new schedule.

    Finishing is unconditional so the region can never stay RUNNING.

    Parameters
    ----------
    state : MaintenanceState
        Current maintenance state.
    next_due : datetime | None
        When the next maintenance check is scheduled.

    Returns
    -------
    MaintenanceIdle
        A fresh IDLE state carrying ``next_due``.
    """
    return MaintenanceIdle(next_due=next_due)
