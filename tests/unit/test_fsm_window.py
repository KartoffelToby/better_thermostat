"""Pure tests for the window FSM (debounced open/close transitions)."""

from custom_components.better_thermostat.core.fsm.window import (
    WindowParams,
    WindowPhase,
    WindowState,
    step,
)

P = WindowParams(open_delay_s=10.0, close_delay_s=30.0)


def test_initial_state_is_closed():
    """A fresh region starts closed and not effectively open."""
    state = WindowState()
    assert state.phase == WindowPhase.CLOSED
    assert state.effective_open is False


def test_open_commits_only_after_delay():
    """An open reading enters OPENING and commits after open_delay_s."""
    state = step(WindowState(), sensor_open=True, now=100.0, params=P)
    assert state.phase == WindowPhase.OPENING
    assert state.effective_open is False

    state = step(state, sensor_open=True, now=105.0, params=P)
    assert state.phase == WindowPhase.OPENING

    state = step(state, sensor_open=True, now=110.0, params=P)
    assert state.phase == WindowPhase.OPEN
    assert state.effective_open is True


def test_open_false_positive_cancels():
    """Closing again before the delay returns to CLOSED without opening."""
    state = step(WindowState(), sensor_open=True, now=100.0, params=P)
    state = step(state, sensor_open=False, now=104.0, params=P)
    assert state.phase == WindowPhase.CLOSED
    assert state.effective_open is False


def test_close_commits_only_after_delay():
    """A close reading enters CLOSING; control still sees the window open."""
    state = WindowState(phase=WindowPhase.OPEN)
    state = step(state, sensor_open=False, now=200.0, params=P)
    assert state.phase == WindowPhase.CLOSING
    assert state.effective_open is True

    state = step(state, sensor_open=False, now=230.0, params=P)
    assert state.phase == WindowPhase.CLOSED
    assert state.effective_open is False


def test_close_false_positive_cancels():
    """Reopening during CLOSING returns to OPEN."""
    state = WindowState(phase=WindowPhase.OPEN)
    state = step(state, sensor_open=False, now=200.0, params=P)
    state = step(state, sensor_open=True, now=210.0, params=P)
    assert state.phase == WindowPhase.OPEN
    assert state.effective_open is True


def test_zero_delay_commits_immediately():
    """With zero delays a change commits in the same step."""
    fast = WindowParams()
    state = step(WindowState(), sensor_open=True, now=1.0, params=fast)
    assert state.phase == WindowPhase.OPEN
    state = step(state, sensor_open=False, now=2.0, params=fast)
    assert state.phase == WindowPhase.CLOSED


def test_steady_inputs_are_idempotent():
    """Repeating the committed reading does not change the state."""
    state = WindowState(phase=WindowPhase.OPEN)
    assert step(state, sensor_open=True, now=500.0, params=P) == state
    closed = WindowState()
    assert step(closed, sensor_open=False, now=500.0, params=P) == closed
