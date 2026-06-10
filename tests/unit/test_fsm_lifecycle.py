"""Pure tests for the lifecycle FSM (startup, grace, running, stopping)."""

from datetime import UTC, datetime, timedelta

from custom_components.better_thermostat.core.fsm.lifecycle import (
    LifecyclePhase,
    LifecycleState,
    startup_finished,
    stop,
    tick,
)

NOW = datetime(2026, 1, 10, 6, 0, tzinfo=UTC)
GRACE_END = NOW + timedelta(minutes=15)


def test_initial_state_is_initialising():
    """A fresh region is INITIALISING with startup running and in grace."""
    state = LifecycleState()
    assert state.phase == LifecyclePhase.INITIALISING
    assert state.startup_running is True
    # Grace only counts once a deadline is set after startup.
    assert state.in_grace(NOW) is False


def test_startup_finished_enters_grace():
    """Finishing startup moves to STARTING with the grace deadline."""
    state = startup_finished(LifecycleState(), grace_until=GRACE_END)
    assert state.phase == LifecyclePhase.STARTING
    assert state.startup_running is False
    assert state.in_grace(NOW) is True
    assert state.in_grace(GRACE_END) is False


def test_startup_finished_is_idempotent_after_starting():
    """A second startup_finished does not reset a later phase."""
    state = startup_finished(LifecycleState(), grace_until=GRACE_END)
    running = tick(state, GRACE_END)
    assert startup_finished(running, grace_until=GRACE_END) == running


def test_tick_promotes_to_running_after_grace():
    """The grace window expiring promotes STARTING to RUNNING."""
    state = startup_finished(LifecycleState(), grace_until=GRACE_END)
    assert tick(state, NOW) == state
    promoted = tick(state, GRACE_END)
    assert promoted.phase == LifecyclePhase.RUNNING
    assert promoted.in_grace(GRACE_END) is False


def test_stop_is_terminal():
    """stop() moves any phase to STOPPING."""
    assert stop(LifecycleState()).phase == LifecyclePhase.STOPPING
    running = LifecycleState(phase=LifecyclePhase.RUNNING)
    assert stop(running).phase == LifecyclePhase.STOPPING


def test_extend_grace_updates_the_deadline_while_starting():
    """extend_grace sets the annunciation deadline in STARTING."""
    from custom_components.better_thermostat.core.fsm.lifecycle import extend_grace

    state = startup_finished(LifecycleState())
    assert state.grace_until is None
    state = extend_grace(state, GRACE_END)
    assert state.phase == LifecyclePhase.STARTING
    assert state.grace_until == GRACE_END


def test_extend_grace_is_a_noop_outside_starting():
    """extend_grace leaves other phases untouched."""
    from custom_components.better_thermostat.core.fsm.lifecycle import extend_grace

    running = LifecycleState(phase=LifecyclePhase.RUNNING)
    assert extend_grace(running, GRACE_END) == running
    stopped = stop(LifecycleState())
    assert extend_grace(stopped, GRACE_END) == stopped
