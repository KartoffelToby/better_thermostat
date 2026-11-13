"""Pure tests for the maintenance FSM (schedule, run guard, liveness)."""

from datetime import UTC, datetime, timedelta

from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.maintenance import (
    DUE_AT_FIRST_TICK,
    MaintenanceDue,
    MaintenanceIdle,
    MaintenanceRunning,
    evaluate_tick,
    finish_run,
    reschedule,
    start_run,
)

NOW = datetime(2026, 1, 10, 3, 0, tzinfo=UTC)
PAST_DUE = NOW - timedelta(minutes=1)


def test_initial_state_idle_not_blocking():
    """A fresh region is idle and never blocks control."""
    state = KernelState().maintenance
    assert state == MaintenanceIdle(next_due=DUE_AT_FIRST_TICK)
    assert state.is_blocking(now_monotonic=0.0) is False


def test_initial_state_arms_at_the_first_tick():
    """A region without a schedule of its own is due at the first tick."""
    out = evaluate_tick(
        KernelState().maintenance, NOW, window_open=False, has_enabled_trvs=True
    )
    assert out == MaintenanceDue(next_due=DUE_AT_FIRST_TICK)


def test_not_due_stays_idle():
    """Before the schedule, ticks change nothing."""
    state = MaintenanceIdle(next_due=NOW + timedelta(hours=2))
    out = evaluate_tick(state, NOW, window_open=False, has_enabled_trvs=True)
    assert out == state


def test_due_arms_the_region():
    """A due schedule moves the region to DUE."""
    state = MaintenanceIdle(next_due=PAST_DUE)
    out = evaluate_tick(state, NOW, window_open=False, has_enabled_trvs=True)
    assert isinstance(out, MaintenanceDue)


def test_window_open_postpones_an_hour():
    """An open window postpones by one hour instead of arming."""
    state = MaintenanceIdle(next_due=PAST_DUE)
    out = evaluate_tick(state, NOW, window_open=True, has_enabled_trvs=True)
    assert isinstance(out, MaintenanceIdle)
    assert out.next_due == NOW + timedelta(hours=1)


def test_released_devices_postpone_an_hour():
    """A due exercise waits while the devices are released.

    It writes to them, and the release ends sooner or later; it does not run
    in the same breath as the devices are taken back either.
    """
    state = MaintenanceIdle(next_due=PAST_DUE)
    out = evaluate_tick(
        state, NOW, window_open=False, has_enabled_trvs=True, devices_released=True
    )
    assert isinstance(out, MaintenanceIdle)
    assert out.next_due == NOW + timedelta(hours=1)


def test_hvac_off_does_not_postpone():
    """OFF mode arms the region: that is when a valve is most likely to seize."""
    out = evaluate_tick(
        MaintenanceIdle(next_due=PAST_DUE),
        NOW,
        window_open=False,
        has_enabled_trvs=True,
    )
    assert isinstance(out, MaintenanceDue)


def test_no_enabled_trvs_schedules_a_week_out():
    """Without maintenance-enabled TRVs the next check moves a week out."""
    out = evaluate_tick(
        MaintenanceIdle(next_due=PAST_DUE),
        NOW,
        window_open=False,
        has_enabled_trvs=False,
    )
    assert isinstance(out, MaintenanceIdle)
    assert out.next_due == NOW + timedelta(days=7)


def test_start_requires_due():
    """start_run is a no-op unless the region is armed."""
    idle = MaintenanceIdle(next_due=NOW)
    assert start_run(idle, now_monotonic=100.0) == idle

    due = MaintenanceDue(next_due=PAST_DUE)
    running = start_run(due, now_monotonic=100.0)
    assert isinstance(running, MaintenanceRunning)
    assert running.running_since == 100.0
    assert running.is_blocking(now_monotonic=150.0) is True


def test_no_double_start():
    """A RUNNING region cannot be started again."""
    running = start_run(MaintenanceDue(next_due=PAST_DUE), 100.0)
    assert start_run(running, 200.0) == running


def test_finish_always_returns_to_idle():
    """finish_run unconditionally reschedules and idles the region."""
    running = start_run(MaintenanceDue(next_due=PAST_DUE), 100.0)
    out = finish_run(running, next_due=NOW + timedelta(days=14))
    assert isinstance(out, MaintenanceIdle)
    assert out.next_due == NOW + timedelta(days=14)
    assert out.is_blocking(now_monotonic=999.0) is False


def test_running_cannot_block_forever():
    """Invariant: a stale RUNNING phase stops blocking after max_run_seconds."""
    running = start_run(MaintenanceDue(next_due=PAST_DUE), 100.0)
    assert running.is_blocking(now_monotonic=100.0 + 3599.0) is True
    assert running.is_blocking(now_monotonic=100.0 + 3600.0) is False


def test_tick_leaves_non_idle_phases_alone():
    """evaluate_tick never advances a DUE or RUNNING region."""
    due = MaintenanceDue(next_due=PAST_DUE)
    out = evaluate_tick(due, NOW, window_open=True, has_enabled_trvs=False)
    assert out == due

    running = start_run(MaintenanceDue(next_due=PAST_DUE), 100.0)
    out = evaluate_tick(running, NOW, window_open=False, has_enabled_trvs=True)
    assert out == running


def test_reschedule_keeps_the_phase_and_the_run_start():
    """Rescheduling moves only next_due; a run keeps its start."""
    running = start_run(MaintenanceDue(next_due=PAST_DUE), 100.0)
    out = reschedule(running, NOW + timedelta(days=3))
    assert out == MaintenanceRunning(
        running_since=100.0, next_due=NOW + timedelta(days=3)
    )
    assert reschedule(MaintenanceIdle(next_due=PAST_DUE), NOW) == MaintenanceIdle(
        next_due=NOW
    )
