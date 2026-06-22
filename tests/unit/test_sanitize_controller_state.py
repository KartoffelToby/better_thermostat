"""Controllers heal poisoned state before computing.

A non-finite number anywhere in a learned state poisons every value
derived from it; the sanitize step discards or resets the affected
state so the controller relearns from live data instead of feeding
NaN into every subsequent cycle.
"""

import logging
import math

from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    compute_mpc,
    sanitize_mpc_state,
)
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    PIDState,
    compute_pid,
    sanitize_pid_state,
)
from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    TpiState,
    compute_tpi,
    sanitize_tpi_state,
)


class TestSanitizeMpcState:
    """MPC discards the whole learned model on any non-finite value."""

    def test_healthy_state_passes_through_unchanged(self):
        """A finite state is returned as-is."""
        state = MpcState(gain_est=0.02, last_percent=40.0)
        healed, pathology = sanitize_mpc_state(state)
        assert healed is state
        assert pathology is None

    def test_non_finite_scalar_discards_the_state(self):
        """NaN in a learned scalar resets the whole model."""
        state = MpcState(gain_est=math.nan, last_percent=40.0)
        healed, pathology = sanitize_mpc_state(state)
        assert pathology == "non-finite state"
        assert healed.gain_est is None
        assert healed.last_percent is None

    def test_non_finite_inside_the_error_deque_is_caught(self):
        """The sweep reaches into containers."""
        state = MpcState()
        state.recent_errors.append(math.inf)
        healed, pathology = sanitize_mpc_state(state)
        assert pathology == "non-finite state"
        assert len(healed.recent_errors) == 0

    def test_non_finite_inside_the_perf_curve_is_caught(self):
        """Nested dicts are part of the learned state too."""
        state = MpcState(perf_curve={"p20_30": {"sum": math.nan, "n": 3}})
        healed, pathology = sanitize_mpc_state(state)
        assert pathology == "non-finite state"
        assert healed.perf_curve == {}


class TestSanitizeTpiState:
    """TPI drops a non-finite remnant and rederives from live readings."""

    def test_healthy_state_passes_through_unchanged(self):
        """A finite TPI state is returned as-is."""
        state = TpiState(last_percent=30.0)
        healed, pathology = sanitize_tpi_state(state)
        assert healed is state
        assert pathology is None

    def test_non_finite_scalar_discards_the_state(self):
        """NaN in the last duty cycle resets the state."""
        state = TpiState(last_percent=math.nan)
        healed, pathology = sanitize_tpi_state(state)
        assert pathology == "non-finite state"
        assert healed.last_percent is None


class TestSanitizePidState:
    """PID heals field by field instead of discarding the whole state."""

    def test_healthy_state_passes_through_unchanged(self):
        """Finite values within bounds report no pathology."""
        state = PIDState(pid_integral=5.0, pid_kp=60.0)
        healed, pathology = sanitize_pid_state(state, PIDParams())
        assert healed is state
        assert pathology is None

    def test_non_finite_integral_resets_to_zero(self):
        """A NaN integrator falls back to its default."""
        state = PIDState(pid_integral=math.nan)
        healed, pathology = sanitize_pid_state(state, PIDParams())
        assert pathology == "non-finite state"
        assert healed.pid_integral == 0.0

    def test_non_finite_gain_resets_to_default(self):
        """A NaN gain falls back to None (= use the configured default)."""
        state = PIDState(pid_kp=math.inf)
        healed, pathology = sanitize_pid_state(state, PIDParams())
        assert pathology == "non-finite state"
        assert healed.pid_kp is None

    def test_runaway_gains_reset_to_defaults(self):
        """Gains far outside their bounds fall back to the defaults."""
        state = PIDState(pid_kp=1e9, pid_ki=0.01, pid_kd=2000.0)
        healed, pathology = sanitize_pid_state(state, PIDParams())
        assert pathology == "runaway gains"
        assert healed.pid_kp is None
        assert healed.pid_ki is None
        assert healed.pid_kd is None

    def test_windup_resets_the_integrator(self):
        """An integrator outside its clamp resets; healthy gains survive."""
        state = PIDState(pid_integral=1e6, pid_kp=60.0, pid_ki=0.01, pid_kd=2000.0)
        healed, pathology = sanitize_pid_state(state, PIDParams())
        assert pathology == "integrator windup"
        assert healed.pid_integral == 0.0
        assert healed.pid_kp == 60.0

    def test_combined_pathologies_all_heal_in_one_pass(self):
        """A non-finite field does not shield runaway gains or windup."""
        state = PIDState(pid_last_meas=math.nan, pid_kp=1e9, pid_integral=1e6)
        healed, pathology = sanitize_pid_state(state, PIDParams())
        assert pathology == "non-finite state"
        assert healed.pid_last_meas is None
        assert healed.pid_kp is None
        assert healed.pid_integral == 0.0


class TestComputeHealsPoisonedState:
    """Each compute entry point sanitizes the resolved state and warns."""

    def test_compute_mpc_replaces_poisoned_state(self, caplog):
        """A poisoned MPC state never reaches the model math."""
        poisoned = MpcState(gain_est=math.nan)
        inp = MpcInput(key="poison_mpc", target_temp_C=22.0, current_temp_C=20.0)
        with caplog.at_level(logging.WARNING):
            output, new_state = compute_mpc(
                inp, MpcParams(), state=poisoned, all_states={}
            )
        assert new_state is not poisoned
        assert new_state.gain_est is None or math.isfinite(new_state.gain_est)
        assert output is not None
        assert math.isfinite(output.valve_percent)
        assert "poisoned MPC state" in caplog.text

    def test_compute_tpi_replaces_poisoned_state(self, caplog):
        """A poisoned TPI state never reaches the duty-cycle math."""
        poisoned = TpiState(last_percent=math.inf)
        inp = TpiInput(key="poison_tpi", current_temp_C=20.0, target_temp_C=22.0)
        with caplog.at_level(logging.WARNING):
            output, new_state = compute_tpi(inp, TpiParams(), state=poisoned)
        assert new_state is not poisoned
        assert output is not None
        assert math.isfinite(output.duty_cycle_pct)
        assert "poisoned TPI state" in caplog.text

    def test_compute_pid_heals_poisoned_state(self, caplog):
        """A poisoned PID state is healed in place before computing."""
        poisoned = PIDState(pid_integral=math.nan)
        with caplog.at_level(logging.WARNING):
            percent, _debug, new_state = compute_pid(
                PIDParams(), 22.0, 20.0, 21.0, 0.0, "poison_pid", state=poisoned
            )
        assert math.isfinite(percent)
        assert math.isfinite(new_state.pid_integral)
        assert "poisoned PID state" in caplog.text
