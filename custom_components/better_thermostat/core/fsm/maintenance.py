"""Maintenance region: idle -> due -> running -> idle (rescheduled).

The region owns the schedule (``next_due``) and the run guard. The
invariant that motivated it: a maintenance run must never be able to
block control permanently — ``is_blocking`` stops honoring a RUNNING
phase once it exceeds the configured maximum runtime, and finishing a
run always reschedules and returns to IDLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

# A valve exercise takes a few minutes per TRV; an hour means it died.
MAX_RUN_S = 3600.0


class MaintenancePhase(StrEnum):
    """Discrete phases of the maintenance region."""

    IDLE = "idle"
    DUE = "due"
    RUNNING = "running"


@dataclass(frozen=True)
class MaintenanceState:
    """State of the maintenance region."""

    phase: MaintenancePhase = MaintenancePhase.IDLE
    next_due: datetime | None = None
    running_since: float | None = None

    def is_blocking(self, now_monotonic: float, max_run_s: float = MAX_RUN_S) -> bool:
        """Whether this region currently pre-empts control.

        A RUNNING phase older than ``max_run_s`` is treated as dead and
        stops blocking, bounding how long maintenance can pre-empt
        control.
        """
        if self.phase != MaintenancePhase.RUNNING:
            return False
        if self.running_since is None:
            return True
        return (now_monotonic - self.running_since) < max_run_s


def evaluate_tick(
    state: MaintenanceState,
    now: datetime,
    *,
    window_open: bool,
    hvac_off: bool,
    has_enabled_trvs: bool,
) -> MaintenanceState:
    """Advance the region on a scheduler tick.

    Postpone rules mirror the shell behavior: an open window or OFF mode
    pushes the schedule out an hour; without any maintenance-enabled TRV
    the next check moves a week out. Otherwise a due schedule arms DUE.

    Parameters
    ----------
    state : MaintenanceState
        Current maintenance state.
    now : datetime
        Current time used to evaluate the schedule.
    window_open : bool
        Whether a window is currently open.
    hvac_off : bool
        Whether the thermostat is in OFF mode.
    has_enabled_trvs : bool
        Whether any TRV has valve maintenance enabled.

    Returns
    -------
    MaintenanceState
        The advanced state (possibly DUE or with a postponed schedule).
    """
    if state.phase != MaintenancePhase.IDLE:
        return state
    if state.next_due is not None and now < state.next_due:
        return state
    if window_open or hvac_off:
        return MaintenanceState(next_due=now + timedelta(hours=1))
    if not has_enabled_trvs:
        return MaintenanceState(next_due=now + timedelta(days=7))
    return MaintenanceState(phase=MaintenancePhase.DUE, next_due=state.next_due)


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
    if state.phase != MaintenancePhase.DUE:
        return state
    return MaintenanceState(
        phase=MaintenancePhase.RUNNING,
        next_due=state.next_due,
        running_since=now_monotonic,
    )


def finish_run(state: MaintenanceState, next_due: datetime | None) -> MaintenanceState:
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
    MaintenanceState
        A fresh IDLE state carrying ``next_due``.
    """
    return MaintenanceState(next_due=next_due)
