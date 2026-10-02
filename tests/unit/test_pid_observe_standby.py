"""PID measurement tracking while actuation is suppressed.

During window-open or OFF ``observe_standby`` keeps the D-channel's
measurement and its timestamp following the room, so the first cycle after
control resumes sees a fresh measurement and a small ``dt``.
"""

from __future__ import annotations

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    observe_standby,
)

_PARAMS = PIDParams(auto_tune=False, d_on_measurement=False)


def test_without_a_reading_the_chain_keeps_its_last_point():
    """No room temperature leaves the measurement and its timestamp alone."""
    state = PIDState(pid_last_meas=20.0, pid_last_time=100.0)

    observe_standby(_PARAMS, state, None, now=500.0)

    assert (state.pid_last_meas, state.pid_last_time) == (20.0, 100.0)


def test_the_filtered_reading_wins_over_the_raw_one():
    """The chain follows the filtered room temperature when there is one."""
    state = PIDState(pid_last_meas=20.0, pid_last_time=100.0)

    observe_standby(_PARAMS, state, 21.0, now=500.0, inp_current_temp_ema_C=20.5)

    assert (state.pid_last_meas, state.pid_last_time) == (20.5, 500.0)


def test_the_filtered_reading_alone_moves_the_chain():
    """A filtered reading without a raw one still follows the room."""
    state = PIDState(pid_last_meas=20.0, pid_last_time=100.0)

    observe_standby(_PARAMS, state, None, now=500.0, inp_current_temp_ema_C=20.5)

    assert (state.pid_last_meas, state.pid_last_time) == (20.5, 500.0)
