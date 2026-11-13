"""Pure tests for the maintenance FSM (schedule, run guard, liveness)."""

from datetime import UTC, datetime, timedelta

from custom_components.better_thermostat.core.decide import KernelState
from custom_components.better_thermostat.core.fsm.maintenance import (
    ANOMALY_MAINTENANCE_COOLDOWN_SECONDS,
    DUE_AT_FIRST_TICK,
    MaintenanceDue,
    MaintenanceIdle,
    MaintenanceRunning,
    evaluate_tick,
    finish_run,
    request_now,
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


def test_request_now_arms_idle_and_respects_cooldown():
    """request_now arms an idle region once per cooldown window."""
    idle = MaintenanceIdle(next_due=NOW + timedelta(days=7))
    armed = request_now(idle, NOW, now_monotonic=1000.0, window_open=False)
    assert armed == MaintenanceDue(next_due=NOW, last_anomaly_run_monotonic=1000.0)

    running = start_run(armed, now_monotonic=1005.0)
    assert running.last_anomaly_run_monotonic == 1000.0
    finished = finish_run(running, next_due=NOW + timedelta(days=7))
    assert finished.last_anomaly_run_monotonic == 1000.0

    # Within cooldown window: stays idle
    in_cooldown = request_now(
        finished,
        NOW + timedelta(hours=1),
        now_monotonic=1000.0 + ANOMALY_MAINTENANCE_COOLDOWN_SECONDS - 1.0,
        window_open=False,
    )
    assert in_cooldown == finished

    # After cooldown window: arms again
    after_cooldown = request_now(
        finished,
        NOW + timedelta(days=1, minutes=1),
        now_monotonic=1000.0 + ANOMALY_MAINTENANCE_COOLDOWN_SECONDS,
        window_open=False,
    )
    assert isinstance(after_cooldown, MaintenanceDue)
    assert (
        after_cooldown.last_anomaly_run_monotonic
        == 1000.0 + ANOMALY_MAINTENANCE_COOLDOWN_SECONDS
    )


def test_request_now_leaves_due_and_running_unchanged():
    """request_now is a no-op when maintenance is already due or running."""
    due = MaintenanceDue(next_due=NOW, last_anomaly_run_monotonic=500.0)
    assert request_now(due, NOW, now_monotonic=90000.0, window_open=False) == due

    running = MaintenanceRunning(
        running_since=600.0, next_due=NOW, last_anomaly_run_monotonic=500.0
    )
    assert (
        request_now(running, NOW, now_monotonic=90000.0, window_open=False) == running
    )


def test_request_now_under_an_open_window_leaves_the_run_to_the_schedule():
    """The exercise drives the valve fully open, which an open window forbids.

    The run becomes due on the schedule, which waits for the window and
    arms it then. No run started, so the cooldown has not started either.
    """
    idle = MaintenanceIdle(next_due=NOW + timedelta(days=7))

    handed_over = request_now(idle, NOW, now_monotonic=1000.0, window_open=True)

    assert handed_over == MaintenanceIdle(next_due=NOW)
    waiting = evaluate_tick(handed_over, NOW, window_open=True, has_enabled_trvs=True)
    assert waiting == MaintenanceIdle(next_due=NOW + timedelta(hours=1))
    armed = evaluate_tick(
        waiting, NOW + timedelta(hours=1), window_open=False, has_enabled_trvs=True
    )
    assert isinstance(armed, MaintenanceDue)


def test_request_now_under_an_open_window_keeps_a_cooldown_running():
    in_cooldown = MaintenanceIdle(
        next_due=NOW + timedelta(days=7), last_anomaly_run_monotonic=900.0
    )

    assert (
        request_now(in_cooldown, NOW, now_monotonic=1000.0, window_open=True)
        == in_cooldown
    )
