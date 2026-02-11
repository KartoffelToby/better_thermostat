"""Tests for the PID controller."""

from custom_components.better_thermostat.utils.calibration.pid import (
    PIDParams,
    build_pid_key,
    compute_pid,
    get_pid_state,
    seed_pid_gains,
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
        # Disable hold-time to allow immediate output changes
        params = PIDParams(
            auto_tune=False, kp=10.0, ki=0.1, kd=5.0, min_hold_time_s=0.0
        )
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

        # Second call with same error; integer rounding may mask tiny increments
        percent2, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key="test_basic",
        )
        assert percent2 >= percent1

        # After a few iterations the integral term should raise the output
        percent_last = percent2
        for _ in range(6):
            percent_last, _ = compute_pid(
                params=params,
                inp_target_temp_C=22.0,
                inp_current_temp_C=20.0,
                inp_trv_temp_C=21.0,
                inp_temp_slope_K_per_min=0.0,
                key="test_basic",
            )
        assert percent_last > percent1

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

        # First call: positive error > band
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        # Second call: overshoot (negative error < band)
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=22.05,  # error = -0.05 < band
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

        # Large error < 1.0, small slope -> sluggish
        compute_pid(
            params=params,
            inp_target_temp_C=20.8,  # error = 0.8 < 1.0
            inp_current_temp_C=20.0,  # error = 0.8 > band
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

    def test_auto_tune_no_tune_due_to_interval(self):
        """Test that auto-tuning is skipped if interval is too short after a previous tune."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=10.0,  # Long interval
            overshoot_threshold_K=0.5,
            steady_state_band_K=0.1,
            sluggish_slope_threshold_K_min=0.005,
        )
        key = "test_interval"

        # First call: sluggish -> tune
        compute_pid(
            params=params,
            inp_target_temp_C=20.8,  # error = 0.8 < 1.0
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,  # < threshold
            key=key,
        )

        state_after_first = get_pid_state(key)
        assert state_after_first is not None
        kp_after_first = state_after_first.pid_kp
        ki_after_first = state_after_first.pid_ki
        kd_after_first = state_after_first.pid_kd
        assert ki_after_first is not None
        assert ki_after_first > params.ki  # Should have increased due to sluggish

        # Immediate second call with sluggish again - should not tune due to interval
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,  # same error
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        state_after_second = get_pid_state(key)
        assert state_after_second is not None
        # Gains should remain the same as after first tune
        assert state_after_second.pid_kp == kp_after_first
        assert state_after_second.pid_ki == ki_after_first
        assert state_after_second.pid_kd == kd_after_first

    def test_auto_tune_gain_clamping(self):
        """Test that gains are clamped to min/max values."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=0.0,
            overshoot_threshold_K=0.1,  # Low threshold to trigger easily
            kp_min=5.0,
            kp_max=20.0,
            kd_min=50.0,
            kd_max=200.0,
            kp_step_mul=0.5,  # Aggressive reduction
            kd_step_mul=2.0,  # Aggressive increase
        )
        key = "test_clamp"

        # Initial gains within range
        initial_kp = 15.0
        initial_kd = 100.0
        params.kp = initial_kp
        params.kd = initial_kd

        # Trigger overshoot multiple times to push gains to limits
        for _ in range(5):
            # Positive error
            compute_pid(
                params=params,
                inp_target_temp_C=22.0,
                inp_current_temp_C=20.0,
                inp_trv_temp_C=21.0,
                inp_temp_slope_K_per_min=0.0,
                key=key,
            )
            # Overshoot
            compute_pid(
                params=params,
                inp_target_temp_C=22.0,
                inp_current_temp_C=22.2,  # error = -0.2 > threshold
                inp_trv_temp_C=21.0,
                inp_temp_slope_K_per_min=0.0,
                key=key,
            )

        state = get_pid_state(key)
        # kp should be clamped to min, kd to max
        assert state is not None
        assert state.pid_kp >= params.kp_min
        assert state.pid_kd <= params.kd_max

    def test_auto_tune_combined_conditions(self):
        """Test auto-tuning with overlapping conditions (overshoot and sluggish)."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=0.0,
            overshoot_threshold_K=0.5,
            steady_state_band_K=0.05,
            sluggish_slope_threshold_K_min=0.01,
            kp_step_mul=0.9,
            ki_step_mul_up=1.2,
            ki_step_mul_down=1.0,  # No change in steady_state
            kp=100.0,
        )
        key = "test_combined"

        # First: sluggish (error <1.0, small slope)
        compute_pid(
            params=params,
            inp_target_temp_C=20.8,  # error = 0.8
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.005,  # sluggish
            key=key,
        )

        state_after_sluggish = get_pid_state(key)
        assert state_after_sluggish is not None
        ki_after_sluggish = state_after_sluggish.pid_ki
        assert ki_after_sluggish is not None
        assert ki_after_sluggish > params.ki  # Increased

        # Second: overshoot (previous abs > band, current abs < band)
        compute_pid(
            params=params,
            inp_target_temp_C=20.8,  # error = 0.8 > band
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.02,  # > threshold, no sluggish
            key=key,
        )
        compute_pid(
            params=params,
            inp_target_temp_C=20.8,
            inp_current_temp_C=20.83,  # error = -0.03 < band
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        state_after_combined = get_pid_state(key)
        assert state_after_combined is not None
        assert state_after_combined.pid_ki is not None
        assert state_after_combined.pid_kp is not None
        assert (
            state_after_combined.pid_ki == ki_after_sluggish
        )  # No further change to ki
        assert state_after_combined.pid_kp < params.kp  # Decreased due to overshoot

    def test_auto_tune_stability_over_cycles(self):
        """Test stability of auto-tuning over multiple cycles without oscillation."""
        params = PIDParams(
            auto_tune=True,
            tune_min_interval_s=0.0,
            overshoot_threshold_K=0.3,
            steady_state_band_K=0.1,
            kp_step_mul=0.95,  # Conservative
            ki_step_mul_up=1.05,
            ki_step_mul_down=0.95,
        )
        key = "test_stability"

        kp_values = []
        ki_values = []

        # Simulate 10 cycles with varying errors
        for i in range(10):
            error = 2.0 if i % 2 == 0 else -0.5  # Alternate positive and overshoot
            current_temp = 20.0 + (2.0 - error)  # Adjust to create error
            compute_pid(
                params=params,
                inp_target_temp_C=22.0,
                inp_current_temp_C=current_temp,
                inp_trv_temp_C=21.0,
                inp_temp_slope_K_per_min=0.0,
                key=key,
            )
            state = get_pid_state(key)
            assert state is not None
            assert state.pid_kp is not None
            assert state.pid_ki is not None
            kp_values.append(state.pid_kp)
            ki_values.append(state.pid_ki)

        # Check that gains don't oscillate wildly (variance should be low)
        kp_variance = sum(
            (x - sum(kp_values) / len(kp_values)) ** 2 for x in kp_values
        ) / len(kp_values)
        ki_variance = sum(
            (x - sum(ki_values) / len(ki_values)) ** 2 for x in ki_values
        ) / len(ki_values)
        assert kp_variance < 10.0  # Arbitrary threshold for stability
        assert ki_variance < 0.01

    def test_derivative_on_measurement(self):
        """Test derivative on measurement with smoothing."""
        params = PIDParams(
            auto_tune=False, d_on_measurement=True, d_smoothing_alpha=0.5
        )
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
        # Check that smoothed measurement is calculated
        assert debug["meas_smooth_C"] is not None
        assert debug["meas_current_used"] is not None
        assert debug["meas_external_raw"] is not None

    def test_hold_time_blocks_small_changes(self):
        """Test that hold-time blocks small output changes within the hold period."""
        # Use short hold time for testing, but long enough to block
        params = PIDParams(
            auto_tune=False,
            kp=10.0,
            ki=0.0,
            kd=0.0,
            min_hold_time_s=300.0,  # 5 minutes hold time
            big_change_threshold_pct=33.0,
        )
        key = "test_hold_block"

        # First call establishes baseline
        percent1, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,  # Error = 2.0, P = 20%
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        assert percent1 == 20.0  # P-term only: 10 * 2.0 = 20

        # Second call with slightly different error (small change < 33%)
        percent2, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.5,  # Error = 1.5, P = 15%
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        # Change would be 20 -> 15 = -5%, which is < 33%, so blocked by hold-time
        assert percent2 == 20.0  # Stays at previous value

    def test_hold_time_allows_big_changes(self):
        """Test that big changes bypass the hold-time restriction."""
        params = PIDParams(
            auto_tune=False,
            kp=10.0,
            ki=0.0,
            kd=0.0,
            min_hold_time_s=300.0,
            big_change_threshold_pct=33.0,
        )
        key = "test_hold_big"

        # First call establishes baseline
        percent1, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,  # Error = 2.0, P = 20%
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        assert percent1 == 20.0

        # Second call with large error change (big change >= 33%)
        percent2, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=15.0,  # Error = 7.0, P = 70%
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        # Change is 20 -> 70 = +50%, which is >= 33%, so bypasses hold-time
        assert percent2 == 70.0  # Big change allowed

    def test_hold_time_allows_target_temp_change(self):
        """Test that target temperature changes bypass the hold-time restriction."""
        params = PIDParams(
            auto_tune=False,
            kp=10.0,
            ki=0.0,
            kd=0.0,
            min_hold_time_s=300.0,
            big_change_threshold_pct=33.0,
        )
        key = "test_hold_target"

        # First call establishes baseline
        percent1, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,  # Error = 2.0, P = 20%
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        assert percent1 == 20.0

        # Second call with changed target temperature
        percent2, _ = compute_pid(
            params=params,
            inp_target_temp_C=23.0,  # Target changed by 1.0°C (> 0.05)
            inp_current_temp_C=20.0,  # Error = 3.0, P = 30%
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        # Change is 20 -> 30 = +10% (< 33%), but target changed so bypasses hold-time
        assert percent2 == 30.0  # Target change allowed

    def test_hold_time_zero_disables_blocking(self):
        """Test that min_hold_time_s=0 disables hold-time blocking."""
        params = PIDParams(
            auto_tune=False,
            kp=10.0,
            ki=0.0,
            kd=0.0,
            min_hold_time_s=0.0,  # Disabled
            big_change_threshold_pct=33.0,
        )
        key = "test_hold_disabled"

        # First call
        percent1, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        assert percent1 == 20.0

        # Second call with small change - should NOT be blocked
        percent2, _ = compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.5,  # Small change
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )
        # With hold-time disabled, small changes go through immediately
        assert percent2 == 15.0  # 10 * 1.5 = 15

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

    def test_seed_pid_gains_creates_new_state(self):
        """Test that seed_pid_gains creates a new state if key doesn't exist."""
        key = "test_seed_new"

        # Ensure state doesn't exist
        assert get_pid_state(key) is None

        # Seed gains
        result = seed_pid_gains(key, kp=15.0, ki=0.05, kd=300.0)

        assert result is True
        state = get_pid_state(key)
        assert state is not None
        assert state.pid_kp == 15.0
        assert state.pid_ki == 0.05
        assert state.pid_kd == 300.0

    def test_seed_pid_gains_updates_existing_state(self):
        """Test that seed_pid_gains updates an existing state."""
        key = "test_seed_update"

        # Create initial state with different values
        seed_pid_gains(key, kp=10.0, ki=0.01, kd=100.0)
        state_before = get_pid_state(key)
        assert state_before.pid_kp == 10.0

        # Update with new values
        result = seed_pid_gains(key, kp=20.0, ki=0.02, kd=200.0)

        assert result is True
        state_after = get_pid_state(key)
        assert state_after.pid_kp == 20.0
        assert state_after.pid_ki == 0.02
        assert state_after.pid_kd == 200.0

    def test_seed_pid_gains_preserves_other_state_fields(self):
        """Test that seed_pid_gains only updates gains, not other fields."""
        key = "test_seed_preserve"
        params = PIDParams(auto_tune=False, kp=10.0, ki=0.1, kd=50.0)

        # Run PID to create state with integral value
        compute_pid(
            params=params,
            inp_target_temp_C=22.0,
            inp_current_temp_C=20.0,
            inp_trv_temp_C=21.0,
            inp_temp_slope_K_per_min=0.0,
            key=key,
        )

        state_before = get_pid_state(key)
        integral_before = state_before.pid_integral

        # Seed new gains
        seed_pid_gains(key, kp=25.0, ki=0.08, kd=400.0)

        state_after = get_pid_state(key)
        # Gains should be updated
        assert state_after.pid_kp == 25.0
        assert state_after.pid_ki == 0.08
        assert state_after.pid_kd == 400.0
        # Integral should be preserved
        assert state_after.pid_integral == integral_before
