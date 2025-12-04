"""Tests for the PID controller."""

import pytest
from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    compute_pid,
    get_pid_state,
    build_pid_key,
)


class TestPIDController:
    """Test cases for PID controller."""

    def setup_method(self):
        """Reset PID states before each test."""
        # Reset all states to ensure clean tests
        import custom_components.better_thermostat.utils.calibration.pid as pid_module

        pid_module._PID_STATES.clear()

    def test_no_temperatures(self):
        """Test behavior when temperatures are missing."""
        params = PIDParams()
        percent, debug = compute_pid(
            params=params,
            inp_target_temp_C=None,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=None,
            inp_temp_slope_K_per_min=None,
            key="test_no_temp",
        )
        assert percent == 0.0
        assert debug["mode"] == "pid"
        assert debug["error"] == "no_temps"

    def test_basic_pid_calculation(self):
        """Test basic PID calculation without auto-tuning."""
        params = PIDParams(auto_tune=False, kp=10.0, ki=0.1, kd=5.0)
        # First call to initialize
        percent1, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key="test_basic",
        )
        # Error = 2.0, P = 10*2 = 20, I accumulates, D=0
        assert percent1 > 0

        # Second call with same error, I should accumulate
        percent2, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key="test_basic",
        )
        assert percent2 > percent1  # Due to integral accumulation

    def test_anti_windup(self):
        """Test anti-windup clamping."""
        params = PIDParams(auto_tune=False, kp=100.0, ki=10.0, i_min=-10.0, i_max=10.0)
        # Large error to cause windup
        percent, _ = compute_pid(
            params=params,
            inp_target_temp_C=30.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key="test_windup",
        )
        # Should be clamped to 100%
        assert percent == 100.0
        # Check that integral is clamped
        state = get_pid_state("test_windup")
        assert state.pid_integral <= params.i_max

    def test_auto_tune_overshoot(self):
        """Test auto-tuning on overshoot."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=0.0,  # Allow immediate tuning
            overshoot_threshold_K=0.5,
            kp_step_mul=0.9,
            kd_step_mul=1.1,
        )
        key = "test_overshoot"

        # First call: positive error
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        # Second call: overshoot (negative error > threshold)
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=22.6,  # error = -0.6
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        state = get_pid_state(key)
        # kp should be reduced, kd increased
        assert state.pid_kp < params.kp
        assert state.pid_kd > params.kd

    def test_auto_tune_sluggish(self):
        """Test auto-tuning for sluggish response."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=0.0,
            steady_state_band_K=0.05,
            sluggish_slope_threshold_K_min=0.01,
            ki_step_mul_up=1.2,
        )
        key = "test_sluggish"

        # Large error, small slope -> sluggish
        compute_pid(
            params=params,
            inp_target_temp_C=25.0,
            inp_current_temp_C=20.0,  # error = 5.0 > band
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.005,  # < threshold
            key=key,
        )

        state = get_pid_state(key)
        # ki should be increased
        assert state.pid_ki > params.ki

    def test_auto_tune_steady_state(self):
        """Test auto-tuning in steady state."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=0.0,
            steady_state_band_K=0.2,
            ki_step_mul_down=0.8,
        )
        key = "test_steady"

        # Small error, low percent -> steady state
        compute_pid(
            params=params,
            inp_target_temp_C=20.1,
            inp_current_temp_C=20.0,  # error = 0.1 < band
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        state = get_pid_state(key)
        # ki should be decreased
        assert state.pid_ki < params.ki

    def test_derivative_on_measurement(self):
        """Test derivative on measurement with mixing."""
        params = PIDParams(auto_tune=False, d_on_measurement=True, trend_mix_trv=0.5)
        # First call to initialize
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key="test_deriv",
        )
        # Second call to have dt > 0
        _, debug = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key="test_deriv",
        )
        # Check that blended measurement is calculated
        assert debug["meas_blend_C"] is not None
        assert debug["mix_w_external"] == 0.5
        assert debug["mix_w_internal"] == 0.5

    def test_build_pid_key(self):
        """Test key building."""

        class MockBT:
            def __init__(self):
                self.bt_target_temp: float | None = 22.5
                self.unique_id = "test_bt"

        bt = MockBT()
        key = build_pid_key(bt, "climate.test")
        assert key == "test_bt:climate.test:t22.5"

        bt.bt_target_temp = None
        key = build_pid_key(bt, "climate.test")
        assert key == "test_bt:climate.test:tunknown"
