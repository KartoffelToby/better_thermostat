"""Pure tests for the control-mode FSM (annunciation of degradation)."""

from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
    step,
)


def test_initial_state_is_optimal():
    """A fresh region is OPTIMAL and not degraded."""
    state = ControlModeState()
    assert state.mode == ControlMode.OPTIMAL
    assert state.degraded is False
    assert state.unavailable_sensors == ()


def test_unavailable_sensor_degrades():
    """Any unavailable optional sensor marks the region as degraded.

    The ladder rung itself only moves via step_ladder (debounced).
    """
    state = step(ControlModeState(), ["sensor.outdoor"], now=100.0)
    assert state.mode == ControlMode.OPTIMAL
    assert state.degraded is True
    assert state.unavailable_sensors == ("sensor.outdoor",)
    assert state.degraded_since == 100.0


def test_degraded_since_is_kept_while_degraded():
    """The first degradation timestamp survives further degraded steps."""
    state = step(ControlModeState(), ["sensor.outdoor"], now=100.0)
    state = step(state, ["sensor.outdoor", "weather.home"], now=200.0)
    assert state.degraded_since == 100.0
    assert state.unavailable_sensors == ("sensor.outdoor", "weather.home")


def test_recovery_returns_to_optimal():
    """No unavailable sensors returns the region to OPTIMAL and clears it."""
    state = step(ControlModeState(), ["sensor.outdoor"], now=100.0)
    state = step(state, [], now=300.0)
    assert state == ControlModeState()


def test_redegradation_restarts_the_clock():
    """A new degradation after recovery gets a fresh timestamp."""
    state = step(ControlModeState(), ["sensor.outdoor"], now=100.0)
    state = step(state, [], now=300.0)
    state = step(state, ["sensor.outdoor"], now=400.0)
    assert state.degraded_since == 400.0
