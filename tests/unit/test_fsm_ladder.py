"""Pure tests for the fail-soft ladder: debounced down, hysteretic up."""

from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
    LadderParams,
    step as control_mode_step,
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
    # The rung commit does not author degraded_since; step() owns it.
    assert state.degraded_since is None


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
    # Clearing it is step()'s call, once the sensors are actually back.
    assert state.degraded_since == 0.0


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


def test_escalation_mid_debounce_commits_the_shallowest_observed_rung():
    """A deeper target mid-debounce does not drag the commit deeper.

    Room sensor lost at t=0 (pending toward SENSOR_FALLBACK), TRV
    temperatures lost at t=119: the window keeps running under the
    sustained deeper pressure, but commits only the shallowest rung
    observed throughout — HOLD must then earn its own full debounce.
    """
    state = _down(ControlModeState(), now=0.0)
    assert state.pending_target == ControlMode.SENSOR_FALLBACK
    state = _down(state, now=119.0, trv_ok=False)
    assert state.mode == ControlMode.OPTIMAL
    assert state.pending_target == ControlMode.SENSOR_FALLBACK
    # The window elapses with only SENSOR_FALLBACK continuously
    # supported; the deeper HOLD pressure starts its own window.
    state = _down(state, now=120.0, trv_ok=False)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    assert state.pending_target == ControlMode.HOLD
    state = _down(state, now=239.0, trv_ok=False)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    state = _down(state, now=240.0, trv_ok=False)
    assert state.mode == ControlMode.HOLD


def test_second_recovery_commits_the_deepest_observed_rung_first():
    """A later, shallower recovery target does not fast-track OPTIMAL.

    TRV temperature back at t=1000 (pending toward SENSOR_FALLBACK),
    room sensor back at t=1250: the window keeps running under the
    sustained shallower pressure, but commits only the deepest rung
    observed throughout — OPTIMAL must then earn its own full window
    of room-sensor stability.
    """
    state = ControlModeState(mode=ControlMode.HOLD, degraded_since=0.0)
    state = _down(state, now=1000.0, trv_ok=True)
    assert state.pending_target == ControlMode.SENSOR_FALLBACK
    state = _up(state, now=1250.0)
    assert state.mode == ControlMode.HOLD
    assert state.pending_target == ControlMode.SENSOR_FALLBACK
    # The window elapses with only SENSOR_FALLBACK continuously
    # supported; the shallower OPTIMAL pressure starts its own window.
    state = _up(state, now=1300.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    assert state.pending_target == ControlMode.OPTIMAL
    state = _up(state, now=1599.0)
    assert state.mode == ControlMode.SENSOR_FALLBACK
    state = _up(state, now=1600.0)
    assert state.mode == ControlMode.OPTIMAL


def test_hold_recovers_stepwise_to_fallback():
    """From HOLD, regained TRV temperature climbs to SENSOR_FALLBACK."""
    state = ControlModeState(mode=ControlMode.HOLD, degraded_since=0.0)
    state = _down(state, now=1000.0, trv_ok=True)  # room still dead, TRV back
    state = _down(state, now=1300.0, trv_ok=True)
    assert state.mode == ControlMode.SENSOR_FALLBACK


def test_flapping_deeper_targets_do_not_starve_the_downgrade():
    """A target oscillating between deeper rungs still commits a downgrade.

    Room sensor dead, TRV availability flapping with a 60 s period: the
    instantaneous target alternates SENSOR_FALLBACK/HOLD, both deeper
    than OPTIMAL. The window keeps running under sustained deeper
    pressure and commits the shallowest continuously supported rung.
    """
    state = ControlModeState()
    trv_ok = True
    for tick in range(11):
        state = _down(state, now=tick * 60.0, trv_ok=trv_ok)
        trv_ok = not trv_ok
    assert state.mode == ControlMode.SENSOR_FALLBACK


def test_flapping_shallower_targets_do_not_starve_the_upgrade():
    """A target oscillating between shallower rungs still leaves HOLD.

    TRV temperature solidly back, room sensor flapping with a 60 s
    period: the instantaneous target alternates OPTIMAL/SENSOR_FALLBACK,
    both shallower than HOLD. The stability window keeps running and
    commits the deepest continuously supported rung.
    """
    state = ControlModeState(mode=ControlMode.HOLD, degraded_since=0.0)
    room_ok = False
    for tick in range(11):
        state = step_ladder(
            state, room_sensor_ok=room_ok, trv_temp_ok=True, now=tick * 60.0, params=P
        )
        room_ok = not room_ok
    assert state.mode == ControlMode.SENSOR_FALLBACK


def test_degraded_since_survives_rung_changes():
    """The first degradation timestamp survives moving between rungs."""
    state = ControlModeState(mode=ControlMode.SENSOR_FALLBACK, degraded_since=50.0)
    state = _down(state, now=1000.0, trv_ok=False)
    state = _down(state, now=1120.0, trv_ok=False)
    assert state.mode == ControlMode.HOLD
    assert state.degraded_since == 50.0


def test_climbing_back_to_optimal_keeps_a_still_degraded_annunciation():
    """A rung commit must not clear an annunciation that still holds.

    The room sensor returning lets the ladder climb back to OPTIMAL while
    an unrelated optional sensor (outdoor, weather) is still away. The
    annunciation belongs to that sensor, not to the rung — and once
    cleared, step() cannot restore it while the sensor stays gone.
    """
    state = ControlModeState(
        mode=ControlMode.SENSOR_FALLBACK,
        unavailable_sensors=("sensor.outdoor",),
        degraded_since=50.0,
    )
    state = _up(state, now=1000.0)
    state = _up(state, now=1400.0)
    assert state.mode == ControlMode.OPTIMAL
    assert state.degraded_since == 50.0

    # The next availability check finds the sensor still gone and must
    # keep reporting the original start time rather than losing it.
    state = control_mode_step(state, ["sensor.outdoor"], 1500.0)
    assert state.degraded is True
    assert state.degraded_since == 50.0
