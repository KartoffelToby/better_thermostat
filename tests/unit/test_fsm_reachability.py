"""Pure tests for the per-TRV reachability FSM (offline record + backoff)."""

from custom_components.better_thermostat.core.fsm.reachability import (
    RETRY_MAX_S,
    ReachabilityState,
    step,
)


def test_initial_state_is_online():
    """A fresh region is online with no retry schedule."""
    state = ReachabilityState()
    assert state.online is True
    assert state.offline_since is None
    assert state.retry_at is None


def test_going_offline_records_since_and_schedules_retry():
    """The first unavailable observation records the time and first retry."""
    state = step(ReachabilityState(), reported_available=False, now=100.0)
    assert state.online is False
    assert state.offline_since == 100.0
    assert state.retry_count == 0
    assert state.retry_at == 130.0


def test_retry_backoff_doubles_up_to_cap():
    """Each missed retry doubles the backoff, capped at ten minutes."""
    state = step(ReachabilityState(), reported_available=False, now=0.0)
    assert state.retry_at == 30.0
    state = step(state, reported_available=False, now=30.0)
    assert state.retry_count == 1
    assert state.retry_at == 30.0 + 60.0
    state = step(state, reported_available=False, now=90.0)
    assert state.retry_at == 90.0 + 120.0
    for _ in range(10):
        assert state.retry_at is not None
        now = state.retry_at
        state = step(state, reported_available=False, now=now)
        assert state.retry_at is not None
        # Each retry interval is capped at RETRY_MAX_S, even after saturation.
        assert state.retry_at - now <= RETRY_MAX_S


def test_observation_before_retry_window_changes_nothing():
    """Offline observations before the retry window leave the state as is."""
    state = step(ReachabilityState(), reported_available=False, now=100.0)
    assert step(state, reported_available=False, now=110.0) == state


def test_coming_back_online_resets():
    """An available observation fully resets the region."""
    state = step(ReachabilityState(), reported_available=False, now=100.0)
    state = step(state, reported_available=False, now=130.0)
    assert step(state, reported_available=True, now=200.0) == ReachabilityState()
