"""Pure tests for the fail-soft ladder: debounced down, hysteretic up."""

from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
    LadderParams,
    step_ladder,
)

P = LadderParams(down_debounce_s=120.0, up_stability_s=300.0)


def _down(state, now, room_ok=False, trv_ok=True):
    return step_ladder(
        state, room_sensor_ok=room_ok, trv_temp_ok=trv_ok, now=now, params=P
    )


def _up(state, now):
    return step_ladder(state, room_sensor_ok=True, trv_temp_ok=True, now=now, params=P)


def test_initial_state_is_optimal():
    """A fresh ladder sits on OPTIMAL."""
    assert ControlModeState().mode == ControlMode.OPTIMAL


def test_downgrade_commits_after_debounce():
    """Sensor loss moves to SENSOR_FALLBACK only after the debounce."""
    state = _down(ControlModeState(), now=0.0)
    assert state.mode == ControlMode.OPTIMAL  # debounce running
    state = _down(state, now=60.0)
    assert state.mode == ControlMode.OPTIMAL
    state = _down(state, now=120.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    assert state.degraded_since == 120.0


def test_flap_during_debounce_cancels_downgrade():
    """A recovering sensor during the debounce keeps OPTIMAL (no flapping)."""
    state = _down(ControlModeState(), now=0.0)
    state = _up(state, now=60.0)
    assert state.mode == ControlMode.OPTIMAL
    # Loss must persist for the full debounce again.
    state = _down(state, now=70.0)
    state = _down(state, now=170.0)
    assert state.mode == ControlMode.OPTIMAL
    state = _down(state, now=190.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK


def test_hold_when_no_trv_temperature_either():
    """Without any usable temperature the ladder bottoms out on HOLD."""
    state = ControlModeState()
    state = _down(state, now=0.0, trv_ok=False)
    state = _down(state, now=120.0, trv_ok=False)
    assert state.mode == ControlMode.HOLD


def test_upgrade_requires_sustained_recovery():
    """The ladder climbs back only after up_stability_s of recovery."""
    state = ControlModeState(mode=ControlMode.SENSOR_FALLBACK, degraded_since=0.0)
    state = _up(state, now=1000.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK  # stability window running
    state = _up(state, now=1200.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    state = _up(state, now=1300.0)
    assert state.mode == ControlMode.OPTIMAL
    assert state.degraded_since is None


def test_flap_during_recovery_restarts_the_window():
    """A relapse during the stability window restarts the upgrade clock."""
    state = ControlModeState(mode=ControlMode.SENSOR_FALLBACK, degraded_since=0.0)
    state = _up(state, now=1000.0)
    state = _down(state, now=1100.0)  # relapse
    state = _up(state, now=1150.0)
    state = _up(state, now=1400.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    state = _up(state, now=1450.0)
    assert state.mode == ControlMode.OPTIMAL


def test_escalation_mid_debounce_restarts_the_debounce():
    """A deeper target during the debounce does not inherit its credit.

    Room sensor lost at t=0 (pending toward SENSOR_FALLBACK), TRV
    temperatures lost at t=119 (pending toward HOLD): HOLD must persist
    for its own full debounce before it commits.
    """
    state = _down(ControlModeState(), now=0.0)
    assert state.pending_target == ControlMode.SENSOR_FALLBACK
    state = _down(state, now=119.0, trv_ok=False)
    assert state.mode == ControlMode.OPTIMAL
    assert state.pending_target == ControlMode.HOLD
    # One second after the escalation the HOLD condition is 1 s old.
    state = _down(state, now=120.0, trv_ok=False)
    assert state.mode == ControlMode.OPTIMAL
    state = _down(state, now=238.0, trv_ok=False)
    assert state.mode == ControlMode.OPTIMAL
    state = _down(state, now=239.0, trv_ok=False)
    assert state.mode == ControlMode.HOLD


def test_second_recovery_restarts_the_stability_window():
    """A later, deeper recovery target restarts the stability window.

    TRV temperature back at t=1000 (pending toward SENSOR_FALLBACK),
    room sensor back at t=1250 (pending toward OPTIMAL): OPTIMAL only
    commits after the room sensor was stable for the full window.
    """
    state = ControlModeState(mode=ControlMode.HOLD, degraded_since=0.0)
    state = _down(state, now=1000.0, trv_ok=True)
    assert state.pending_target == ControlMode.SENSOR_FALLBACK
    state = _up(state, now=1250.0)
    assert state.mode == ControlMode.HOLD
    assert state.pending_target == ControlMode.OPTIMAL
    # 300 s after the first recovery, but only 50 s of room stability.
    state = _up(state, now=1300.0)
    assert state.mode == ControlMode.HOLD
    state = _up(state, now=1549.0)
    assert state.mode == ControlMode.HOLD
    state = _up(state, now=1550.0)
    assert state.mode == ControlMode.OPTIMAL


def test_hold_recovers_stepwise_to_fallback():
    """From HOLD, regained TRV temperature climbs to SENSOR_FALLBACK."""
    state = ControlModeState(mode=ControlMode.HOLD, degraded_since=0.0)
    state = _down(state, now=1000.0, trv_ok=True)  # room still dead, TRV back
    state = _down(state, now=1300.0, trv_ok=True)
    assert state.mode == ControlMode.SENSOR_FALLBACK


def test_degraded_since_survives_rung_changes():
    """The first degradation timestamp survives moving between rungs."""
    state = ControlModeState(mode=ControlMode.SENSOR_FALLBACK, degraded_since=50.0)
    state = _down(state, now=1000.0, trv_ok=False)
    state = _down(state, now=1120.0, trv_ok=False)
    assert state.mode == ControlMode.HOLD
    assert state.degraded_since == 50.0
