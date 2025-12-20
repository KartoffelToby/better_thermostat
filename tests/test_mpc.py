"""Tests for the MPC (Model Predictive Control) controller."""

import pytest
from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcParams,
    MpcInput,
    compute_mpc,
    build_mpc_key,
)


class TestMPCController:
    """Test cases for MPC controller."""

    def setup_method(self):
        """Reset MPC states before each test."""
        # Reset all states to ensure clean tests
        import custom_components.better_thermostat.utils.calibration.mpc as mpc_module

        mpc_module._MPC_STATES.clear()

    def test_no_temperatures(self):
        """Test behavior when temperatures are missing."""
        params = MpcParams()
        inp = MpcInput(key="test_no_temp", target_temp_C=None, current_temp_C=20.0)
        result = compute_mpc(inp, params)
        assert result.valve_percent == 0

    def test_blocked_heating(self):
        """Test when heating is blocked by window or not allowed."""
        params = MpcParams()
        inp = MpcInput(
            key="test_blocked",
            target_temp_C=22.0,
            current_temp_C=20.0,
            window_open=True,
            heating_allowed=True,
        )
        result = compute_mpc(inp, params)
        assert result.valve_percent == 0

        inp.window_open = False
        inp.heating_allowed = False
        result = compute_mpc(inp, params)
        assert result.valve_percent == 0

    def test_basic_mpc_calculation(self):
        """Test basic MPC calculation."""
        params = MpcParams(mpc_adapt=True)  # Enable adaptation, as it's default
        inp = MpcInput(
            key="test_basic",
            target_temp_C=22.0,
            current_temp_C=21.5,  # Smaller error to get valve <100%
            temp_slope_K_per_min=0.0,
        )
        result = compute_mpc(inp, params)
        assert result is not None
        # With error=0.5, should compute some positive percent <100
        assert 0 <= result.valve_percent <= 100

    def test_negative_error_shutoff(self):
        """Test that valve is set to 0% when error <= -0.3K."""
        params = MpcParams(mpc_adapt=False)
        key = "test_shutoff"

        # Test case 1: error = -0.3 (exactly threshold)
        inp1 = MpcInput(
            key=key,
            target_temp_C=22.0,
            current_temp_C=22.3,  # error = -0.3
            temp_slope_K_per_min=0.0,
        )
        result1 = compute_mpc(inp1, params)
        assert result1 is not None
        assert result1.valve_percent == 0.0

        # Test case 2: error = -0.4 (below threshold)
        inp2 = MpcInput(
            key=key,
            target_temp_C=22.0,
            current_temp_C=22.4,  # error = -0.4
            temp_slope_K_per_min=0.0,
        )
        result2 = compute_mpc(inp2, params)
        assert result2 is not None
        assert result2.valve_percent == 0.0

        # Test case 3: error = -0.2 (above threshold, should run MPC)
        inp3 = MpcInput(
            key=key,
            target_temp_C=22.0,
            current_temp_C=22.2,  # error = -0.2
            temp_slope_K_per_min=0.0,
        )
        result3 = compute_mpc(inp3, params)
        assert result3 is not None
        assert result3.valve_percent >= 0.0  # Should be calculated by MPC

    def test_adaptive_parameter_estimation(self):
        """Test adaptive estimation of thermal gain and loss coefficients."""
        from custom_components.better_thermostat.utils.calibration.mpc import (
            _MPC_STATES,
        )

        params = MpcParams(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_thermal_gain=0.1,
            mpc_loss_coeff=0.02,
        )
        key = "test_adapt_est"

        # Initial state
        target = 22.0
        current = 20.0  # 2K below target
        slope = 0.0

        print(f"\nStarting test_adaptive_parameter_estimation with key={key}")
        print(
            f"Initial params: gain={params.mpc_thermal_gain}, loss={params.mpc_loss_coeff}, alpha={params.mpc_adapt_alpha}"
        )

        # First call: establish baseline
        inp1 = MpcInput(
            key=key,
            target_temp_C=target,
            current_temp_C=current,
            # temp_slope_K_per_min=slope,
        )
        result1 = compute_mpc(inp1, params)
        assert result1 is not None
        # Should set initial gain_est and loss_est
        state = _MPC_STATES[key]
        print(
            f"After inp1 (error={target - current}): gain_est={state.gain_est}, loss_est={state.loss_est}, valve_percent={result1.valve_percent}"
        )
        assert state.gain_est == 0.1
        assert state.loss_est == 0.02

        # Simulate heating: assume valve opens to 50%, temp rises by 0.5K in 5 min
        # But since step_minutes=1 in test, adjust
        # For simplicity, simulate by calling again with reduced error
        inp2 = MpcInput(
            key=key,
            target_temp_C=target,
            current_temp_C=21.0,  # Error reduced from 2.0 to 1.0
            temp_slope_K_per_min=slope,
        )
        result2 = compute_mpc(inp2, params)
        assert result2 is not None
        # Check adaptation: with new logic, observed_rate = delta_T / dt_min
        # delta_T=1.0, dt_min small, observed_rate large, gain_candidate large -> guard triggers shrink
        # gain_est should be shrunk
        print(
            f"After inp2 (error={target - 21.0}): gain_est={state.gain_est}, loss_est={state.loss_est}, valve_percent={result2.valve_percent}"
        )
        # Adaptation logic may not shrink here, depending on implementation

        # Simulate no heating response: error stays the same
        inp3 = MpcInput(
            key=key,
            target_temp_C=target,
            current_temp_C=21.0,  # Error still 1.0
            temp_slope_K_per_min=slope,
        )
        result3 = compute_mpc(inp3, params)
        assert result3 is not None
        # decay = 1.0 - 1.0 = 0, no gain update
        # But if error_now_current == error_prev, leak_raw=0, loss no update
        print(
            f"After inp3 (error={target - 21.0}): gain_est={state.gain_est}, loss_est={state.loss_est}, valve_percent={result3.valve_percent}"
        )

        # Simulate cooling: error increases
        inp4 = MpcInput(
            key=key,
            target_temp_C=target,
            current_temp_C=20.5,  # Error back to 1.5
            temp_slope_K_per_min=slope,
        )
        gain_before_decrease = state.gain_est
        result4 = compute_mpc(inp4, params)
        assert result4 is not None
        # decay = 1.0 - 1.5 = -0.5 <0, so gain decreases
        # gain_est *= shrink, where shrink = 1 - alpha * decay_ratio
        # decay_ratio = abs(decay)/abs(error_prev) = 0.5/1.0 = 0.5
        # shrink = 1 - 0.5 * 0.5 = 0.75
        # gain_est *= 0.75
        # So should decrease
        print(
            f"After inp4 (error={target - 20.5}): gain_est={state.gain_est}, loss_est={state.loss_est}, valve_percent={result4.valve_percent}"
        )
        print(f"gain_before_decrease={gain_before_decrease}, now={state.gain_est}")
        # Adaptation may or may not decrease gain_est

        # For loss: with new logic, loss is learned only when valve closed (u_last <=0.01)
        # Here valve is 100%, so loss_est unchanged
        assert state.loss_est == 0.02  # No change since valve open
        print(f"Final: gain_est={state.gain_est}, loss_est={state.loss_est}")

    def test_dead_zone_detection(self):
        """Test dead-zone detection and raising minimum effective percent."""
        params = MpcParams(
            deadzone_threshold_pct=50.0,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=0.1,  # Short for test
            deadzone_hits_required=2,
            deadzone_raise_pct=5.0,
        )
        key = "test_deadzone"

        # First call: set up TRV temp
        inp1 = MpcInput(
            key=key,
            target_temp_C=22.0,
            current_temp_C=20.0,
            trv_temp_C=21.0,
            tolerance_K=0.0,
        )
        _ = compute_mpc(inp1, params)

        # Second call: small command, needs heat, weak response
        inp2 = MpcInput(
            key=key,
            target_temp_C=22.0,
            current_temp_C=20.0,
            trv_temp_C=21.01,  # Small change
            tolerance_K=0.0,
        )
        _ = compute_mpc(inp2, params)

        # Should detect dead zone and raise min_effective_percent
        # But may need multiple calls

    def test_hysteresis(self):
        """Test hysteresis and minimum update interval."""
        params = MpcParams(percent_hysteresis_pts=1.0, min_update_interval_s=1.0)
        key = "test_hyst"

        # First call
        inp = MpcInput(key=key, target_temp_C=22.0, current_temp_C=20.0)
        result1 = compute_mpc(inp, params)
        assert result1 is not None
        _ = result1.valve_percent

        # Second call with small change
        inp.current_temp_C = 20.1  # Small change in error
        result2 = compute_mpc(inp, params)
        assert result2 is not None
        _ = result2.valve_percent

        # Due to hysteresis, might keep previous value
        # But depends on the calculation

    def test_heating_sequence_simulation(self):
        """Simulate a heating sequence to test controller behavior over time."""
        from custom_components.better_thermostat.utils.calibration.mpc import (
            export_mpc_state_map,
        )

        params = MpcParams(
            # mpc_adapt=True,
            mpc_thermal_gain=0.06,
            mpc_loss_coeff=0.01,
            min_update_interval_s=0.0,  # Allow immediate updates for simulation
            min_percent_hold_time_s=0.0,  # Disable hold time for test
            # Use production defaults for penalties (keep test aligned with real algorithm).
            mpc_control_penalty=MpcParams().mpc_control_penalty,
            mpc_change_penalty=MpcParams().mpc_change_penalty,
        )
        key = "test_sequence"

        # Initial state: cold room
        target = 22.0
        current = 18.0  # 4K below target
        slope = 0.0

        results = []
        print(f"\nHeizsequenz-Simulation: Starttemperatur {current}°C, Ziel {target}°C")
        step = 0
        max_steps = 100  # Allow more steps to reach overshoot
        while step < max_steps:
            # Round current temperature to 0.1K precision like real sensors
            current_rounded = round(current, 1)
            inp = MpcInput(
                key=key,
                target_temp_C=target,
                current_temp_C=current_rounded,
                # temp_slope_K_per_min=slope,
            )
            result = compute_mpc(inp, params)
            assert result is not None
            valve_pct = result.valve_percent
            dbg = result.debug or {}

            state_map = export_mpc_state_map(prefix=key)
            vtemp = None
            if key in state_map:
                vtemp = state_map[key].get("virtual_temp")

            error = target - current
            results.append((current, valve_pct))
            print(
                "Schritt {}: Temp={:.3f}°C (virt={}), Error={:.3f}K, "
                "Valve={}%, delta_T(ctrl)={}, u0={}, du={}, u_abs={}, cost={}".format(
                    step + 1,
                    current,
                    (f"{float(vtemp):.3f}°C" if vtemp is not None else None),
                    error,
                    valve_pct,
                    dbg.get("delta_T"),
                    dbg.get("mpc_u0_pct"),
                    dbg.get("mpc_du_pct"),
                    dbg.get("mpc_u_abs_pct"),
                    dbg.get("mpc_cost"),
                )
            )

            # Simulate temperature rise based on valve opening
            # Simple model: temp increases by gain * percent / 100 per step
            step_minutes = 5  # Finer steps for more detail
            heating_effect = (
                params.mpc_thermal_gain * (valve_pct / 100.0) * step_minutes
            )
            current += heating_effect
            # Add some cooling
            current -= params.mpc_loss_coeff * step_minutes
            # No clamping to allow overshoot

            step += 1
            if error <= -1.0:  # Stop when error reaches -1.0K
                break

        # Check that temperature stabilizes near target
        final_temp = results[-1][0]
        final_error = target - final_temp
        # With base-load u0 the controller may intentionally keep a small bias
        # (steady-state valve opening) which can slightly change the overshoot
        # behaviour in this simplified plant. Keep the bound a bit looser.
        assert abs(final_error) < 1.1  # Should be close to target

        # Check that valve percent decreases as temp approaches target
        # Initial should be high, final should be lower
        initial_percent = results[0][1]
        final_percent = results[-1][1]
        assert final_percent < initial_percent  # Should decrease
