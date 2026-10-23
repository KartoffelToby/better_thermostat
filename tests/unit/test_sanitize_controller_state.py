"""MPC and TPI self-heal poisoned state before computing.

A non-finite number anywhere in a learned state poisons every
prediction derived from it; the sanitize step discards the state and
the model relearns from live data. PID's equivalent lives in
sanitize_pid_state (see test_calibrator_strategy).
"""

import math

from custom_components.better_thermostat.core.calibrator import CalibratorHealth
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcState,
    sanitize_mpc_state,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiState,
    sanitize_tpi_state,
)


def test_healthy_mpc_state_passes_through_unchanged():
    """A finite state is returned as-is."""
    state = MpcState(gain_est=0.02, last_percent=40.0)
    healed, health = sanitize_mpc_state(state)
    assert healed is state
    assert health == CalibratorHealth.HEALTHY


def test_non_finite_mpc_scalar_discards_the_state():
    """NaN in a learned scalar resets the whole model."""
    state = MpcState(gain_est=math.nan, last_percent=40.0)
    healed, health = sanitize_mpc_state(state)
    assert health == CalibratorHealth.NON_FINITE
    assert healed.gain_est is None
    assert healed.last_percent is None


def test_non_finite_inside_the_error_deque_is_caught():
    """The sweep reaches into containers."""
    state = MpcState()
    state.recent_errors.append(math.inf)
    healed, health = sanitize_mpc_state(state)
    assert health == CalibratorHealth.NON_FINITE
    assert len(healed.recent_errors) == 0


def test_non_finite_inside_the_perf_curve_is_caught():
    """Nested dicts are part of the learned state too."""
    state = MpcState(perf_curve={"20": {"sum": math.nan, "n": 3}})
    healed, health = sanitize_mpc_state(state)
    assert health == CalibratorHealth.NON_FINITE
    assert healed.perf_curve == {}


def test_healthy_tpi_state_passes_through_unchanged():
    """A finite TPI state is returned as-is."""
    state = TpiState(last_percent=30.0)
    healed, health = sanitize_tpi_state(state)
    assert healed is state
    assert health == CalibratorHealth.HEALTHY


def test_non_finite_tpi_percent_discards_the_state():
    """NaN in the duty cycle remnant drops the state."""
    state = TpiState(last_percent=math.nan)
    healed, health = sanitize_tpi_state(state)
    assert health == CalibratorHealth.NON_FINITE
    assert healed.last_percent is None
