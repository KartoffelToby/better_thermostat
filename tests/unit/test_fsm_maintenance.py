"""Pure tests for the maintenance FSM (schedule, run guard, liveness)."""

from datetime import UTC, datetime, timedelta

from custom_components.better_thermostat.core.fsm.maintenance import (
    MaintenancePhase,
    MaintenanceState,
    evaluate_tick,
    finish_run,
    start_run,
)

NOW = datetime(2026, 1, 10, 3, 0, tzinfo=UTC)


def test_initial_state_idle_not_blocking():
    """A fresh region is idle and never blocks control."""
    state = MaintenanceState()
    assert state.phase == MaintenancePhase.IDLE
    assert state.is_blocking(now_monotonic=0.0) is False


def test_not_due_stays_idle():
    """Before the schedule, ticks change nothing."""
    state = MaintenanceState(next_due=NOW + timedelta(hours=2))
    out = evaluate_tick(
        state, NOW, window_open=False, hvac_off=False, has_enabled_trvs=True
    )
    assert out == state


def test_due_arms_the_region():
    """A due schedule moves the region to DUE."""
    state = MaintenanceState(next_due=NOW - timedelta(minutes=1))
    out = evaluate_tick(
        state, NOW, window_open=False, hvac_off=False, has_enabled_trvs=True
    )
    assert out.phase == MaintenancePhase.DUE


def test_window_open_postpones_an_hour():
    """An open window postpones by one hour instead of arming."""
    state = MaintenanceState(next_due=NOW - timedelta(minutes=1))
    out = evaluate_tick(
        state, NOW, window_open=True, hvac_off=False, has_enabled_trvs=True
    )
    assert out.phase == MaintenancePhase.IDLE
    assert out.next_due == NOW + timedelta(hours=1)


def test_hvac_off_postpones_an_hour():
    """OFF mode postpones by one hour instead of arming."""
    out = evaluate_tick(
        MaintenanceState(), NOW, window_open=False, hvac_off=True, has_enabled_trvs=True
    )
    assert out.phase == MaintenancePhase.IDLE
    assert out.next_due == NOW + timedelta(hours=1)


def test_no_enabled_trvs_schedules_a_week_out():
    """Without maintenance-enabled TRVs the next check moves a week out."""
    out = evaluate_tick(
        MaintenanceState(),
        NOW,
        window_open=False,
        hvac_off=False,
        has_enabled_trvs=False,
    )
    assert out.phase == MaintenancePhase.IDLE
    assert out.next_due == NOW + timedelta(days=7)


def test_start_requires_due():
    """start_run is a no-op unless the region is armed."""
    idle = MaintenanceState()
    assert start_run(idle, now_monotonic=100.0) == idle

    due = MaintenanceState(phase=MaintenancePhase.DUE)
    running = start_run(due, now_monotonic=100.0)
    assert running.phase == MaintenancePhase.RUNNING
    assert running.running_since == 100.0
    assert running.is_blocking(now_monotonic=150.0) is True


def test_no_double_start():
    """A RUNNING region cannot be started again."""
    running = start_run(MaintenanceState(phase=MaintenancePhase.DUE), 100.0)
    assert start_run(running, 200.0) == running


def test_finish_always_returns_to_idle():
    """finish_run unconditionally reschedules and idles the region."""
    running = start_run(MaintenanceState(phase=MaintenancePhase.DUE), 100.0)
    out = finish_run(running, next_due=NOW + timedelta(days=14))
    assert out.phase == MaintenancePhase.IDLE
    assert out.next_due == NOW + timedelta(days=14)
    assert out.is_blocking(now_monotonic=999.0) is False


def test_running_cannot_block_forever():
    """Invariant: a stale RUNNING phase stops blocking after max_run_s."""
    running = start_run(MaintenanceState(phase=MaintenancePhase.DUE), 100.0)
    assert running.is_blocking(now_monotonic=100.0 + 3599.0) is True
    assert running.is_blocking(now_monotonic=100.0 + 3600.0) is False


def test_running_without_timestamp_blocks():
    """A RUNNING phase without a start timestamp is treated as blocking."""
    state = MaintenanceState(phase=MaintenancePhase.RUNNING, running_since=None)
    assert state.is_blocking(now_monotonic=99_999.0) is True


def test_tick_leaves_non_idle_phases_alone():
    """evaluate_tick never advances a DUE or RUNNING region."""
    due = MaintenanceState(phase=MaintenancePhase.DUE)
    out = evaluate_tick(
        due, NOW, window_open=True, hvac_off=True, has_enabled_trvs=False
    )
    assert out == due

    running = start_run(MaintenanceState(phase=MaintenancePhase.DUE), 100.0)
    out = evaluate_tick(
        running, NOW, window_open=False, hvac_off=False, has_enabled_trvs=True
    )
    assert out == running
