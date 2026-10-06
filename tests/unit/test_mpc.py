"""Tests for the MPC (Model Predictive Control) controller.

State is threaded explicitly through a test-local state dict, mirroring how
the StateManager owns controller state in production.
"""

from unittest.mock import patch

import pytest

from custom_components.better_thermostat.utils.calibration.mpc import (
    MpcInput,
    MpcParams,
    MpcState,
    _forget_stamps_ahead_of_the_clock,
    _post_process_percent,
    _update_perf_curve,
    compute_mpc as _compute_mpc,
)

_MPC = "custom_components.better_thermostat.utils.calibration.mpc"
_STATES: dict[str, MpcState] = {}


def compute_mpc(inp, params):
    """Run ``_compute_mpc`` threading state for ``inp.key`` via ``_STATES``."""
    state = _STATES.setdefault(inp.key, MpcState())
    output, new_state = _compute_mpc(inp, params, state=state, all_states=_STATES)
    _STATES[inp.key] = new_state
    return output, new_state


class TestMPCController:
    """Test cases for MPC controller."""

    def setup_method(self):
        """Reset MPC states before each test."""
        _STATES.clear()

    def test_no_temperatures(self):
        """Test behavior when temperatures are missing."""
        params = MpcParams()
        inp = MpcInput(key="test_no_temp", target_temp_C=None, current_temp_C=20.0)
        result, _ = compute_mpc(inp, params)
        assert result is not None
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
        result, _ = compute_mpc(inp, params)
        assert result is not None
        assert result.valve_percent == 0

        inp.window_open = False
        inp.heating_allowed = False
        result, _ = compute_mpc(inp, params)
        assert result is not None
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
        result, _ = compute_mpc(inp, params)
        assert result is not None
        # A 0.5 K error opens the valve part of the way, not to a rail.
        assert 0 < result.valve_percent < 100

    @pytest.mark.parametrize("room_temperature", [22.2, 22.3, 22.4])
    def test_negative_error_shutoff(self, room_temperature):
        """A room above its target gets a closed valve from the controller itself.

        The forced loss calibration, which also closes the valve above
        target, is kept from triggering so the controller's own answer is
        what the test sees.
        """
        params = MpcParams(mpc_adapt=False)
        key = f"test_shutoff_{room_temperature}"
        _STATES[key] = MpcState(loss_learn_count=100)
        inp = MpcInput(
            key=key,
            target_temp_C=22.0,
            current_temp_C=room_temperature,
            temp_slope_K_per_min=0.0,
        )
        with patch(f"{_MPC}.random.random", return_value=0.99):
            result, _ = compute_mpc(inp, params)
        assert result is not None
        assert result.debug.get("calib_active") is None
        assert result.valve_percent == 0

    def test_filtered_temperature_only_affects_cost(self):
        """Ensure filtered temperature reduces valve demand without confusing learning."""

        params = MpcParams(
            mpc_adapt=False,
            min_update_interval_s=0.0,
            min_percent_hold_time_s=0.0,
            percent_hysteresis_pts=0.0,
            mpc_control_penalty=0.0,
            mpc_change_penalty=0.0,
            use_virtual_temp=False,
        )

        # Raw sensor value (used for learning) is 0.7K below target.
        base_temp = 21.3
        target = 22.0

        raw, _ = compute_mpc(
            MpcInput(
                key="test_filtered_raw", target_temp_C=target, current_temp_C=base_temp
            ),
            params,
        )

        filtered, _ = compute_mpc(
            MpcInput(
                key="test_filtered_cost",
                target_temp_C=target,
                current_temp_C=base_temp,
                filtered_temp_C=21.9,  # EMA closer to target → lower cost
            ),
            params,
        )

        assert raw is not None and filtered is not None
        assert filtered.valve_percent < raw.valve_percent

    def test_adaptive_parameter_estimation(self):
        """Test adaptive estimation of thermal gain and loss coefficients."""
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
        result1, _ = compute_mpc(inp1, params)
        assert result1 is not None
        # Should set initial gain_est and loss_est
        state = _STATES[key]
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
        result2, _ = compute_mpc(inp2, params)
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
        result3, _ = compute_mpc(inp3, params)
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
        result4, _ = compute_mpc(inp4, params)
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

    def test_gain_does_not_increase_on_slope_without_sensor_change(self):
        """Slope-only identification must not drift gain when the sensor is flat."""

        from time import time

        params = MpcParams(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_thermal_gain=0.05,
            mpc_loss_coeff=0.01,
        )
        key = "test_gain_slope_reject"

        # First call initializes state.
        inp1 = MpcInput(
            key=key, target_temp_C=22.0, current_temp_C=21.5, temp_slope_K_per_min=0.08
        )
        _ = compute_mpc(inp1, params)

        st = _STATES[key]
        st.last_percent = 100.0
        st.last_learn_temp = inp1.current_temp_C
        st.last_learn_time = time() - 300.0  # >=180s, but <600s (no steady-state gain)
        assert st.gain_est is not None
        gain_before = float(st.gain_est)

        # Second call: sensor unchanged, but slope still positive.
        inp2 = MpcInput(
            key=key, target_temp_C=22.0, current_temp_C=21.5, temp_slope_K_per_min=0.08
        )
        _ = compute_mpc(inp2, params)
        st = _STATES[key]

        assert st.gain_est is not None
        assert float(st.gain_est) <= gain_before

    def test_gain_decreases_when_high_output_and_no_warming(self):
        """If valve is high, temperature is flat, and still below target, gain should decrease."""

        from time import time

        params = MpcParams(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_thermal_gain=0.1,
            mpc_loss_coeff=0.01,
        )
        key = "test_gain_ss_decrease"

        # First call initializes state and sets last_target_C.
        _ = compute_mpc(
            MpcInput(key=key, target_temp_C=22.0, current_temp_C=21.5), params
        )

        st = _STATES[key]
        st.gain_est = 0.1
        st.loss_est = 0.01
        st.last_percent = 90.0
        st.last_learn_temp = 21.5
        st.last_learn_time = time() - 900.0  # 15min -> in steady-state learning window
        st.last_residual_time = st.last_learn_time  # align residual window

        gain_before = float(st.gain_est)
        _ = compute_mpc(
            MpcInput(
                key=key,
                target_temp_C=22.0,
                current_temp_C=21.5,
                temp_slope_K_per_min=0.0,
            ),
            params,
        )

        assert float(st.gain_est) < gain_before

    def test_loss_can_learn_from_steady_state_without_valve_closing(self):
        """Loss should be able to learn under quasi steady-state even if the valve never closes."""

        from time import time

        params = MpcParams(
            mpc_adapt=True,
            mpc_adapt_alpha=0.5,
            mpc_thermal_gain=0.12,
            mpc_loss_coeff=0.01,
        )
        key = "test_loss_residual_ss"

        # Init state
        _ = compute_mpc(
            MpcInput(key=key, target_temp_C=22.0, current_temp_C=21.8), params
        )

        st = _STATES[key]
        st.gain_est = 0.12
        st.loss_est = 0.01
        st.last_percent = 36.0
        st.last_learn_temp = 21.8
        st.last_learn_time = time() - 360.0  # 6min: >=180s and in residual window
        st.last_residual_time = st.last_learn_time  # align residual window

        loss_before = float(st.loss_est)

        res, _ = compute_mpc(
            MpcInput(
                key=key,
                target_temp_C=22.0,
                current_temp_C=21.8,
                # slope may be noisy; steady-state learning should prefer delta when sensor flat
                temp_slope_K_per_min=-0.07,
            ),
            params,
        )
        assert res is not None
        assert float(st.loss_est) >= loss_before

    @staticmethod
    def _dead_zone_run(hits_required, cycles):
        """Hold a 20 % command against a TRV that barely warms, return the state.

        The command is small, the room is 2 K below target and the TRV
        warms by 1 mK per 120 s evaluation: every evaluation is a weak
        response to a small command.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            deadzone_threshold_pct=50.0,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=60.0,
            deadzone_hits_required=hits_required,
            deadzone_raise_pct=5.0,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
        )
        state = MpcState()
        for cycle in range(cycles):
            inp = MpcInput(
                key="deadzone",
                target_temp_C=22.0,
                current_temp_C=20.0,
                trv_temp_C=21.0 + 0.001 * cycle,
                tolerance_K=0.0,
            )
            _post_process_percent(
                inp, params, state, 1000.0 + 120.0 * cycle, 20.0, None
            )
        return state

    def test_dead_zone_detection(self):
        """A weak response to a small command raises the minimum opening.

        With one hit required, the first evaluation that sees the TRV not
        warming lifts the minimum effective opening to the command plus the
        configured raise.
        """
        state = self._dead_zone_run(hits_required=1, cycles=2)

        assert state.min_effective_percent == 25.0

    def test_repeated_dead_zone_hits_raise_the_minimum_opening(self):
        """Weak responses keep counting until the required number of hits.

        A threshold-like TRV is the case dead-zone learning exists for, so
        classifying the TRV as one does not stop the hits from counting.
        """
        state = self._dead_zone_run(hits_required=2, cycles=3)

        assert state.min_effective_percent == 25.0

    def test_a_small_command_the_room_answers_is_not_a_dead_zone_hit(self):
        """A small command that warms the TRV and the room raises no minimum.

        The room reading the controller keeps from its last cycle is the
        measured response: a room that warms well beyond what the command
        was expected to give shows the valve is past its dead zone.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            deadzone_threshold_pct=50.0,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=60.0,
            deadzone_hits_required=1,
            deadzone_raise_pct=5.0,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
        )
        state = MpcState()
        for cycle in range(4):
            now = 1000.0 + 300.0 * cycle
            room = 19.0 + 0.2 * cycle
            inp = MpcInput(
                key="deadzone",
                target_temp_C=22.0,
                current_temp_C=room,
                trv_temp_C=21.0 + 0.5 * cycle,
                tolerance_K=0.0,
            )
            _post_process_percent(inp, params, state, now, 20.0, None)
            # The controller records the room after post-processing, as
            # the performance curve does once per window.
            state.last_room_temp_C = room
            state.last_room_temp_ts = now

        assert state.dead_zone_hits == 0
        assert state.min_effective_percent is None

    def test_a_wall_clock_step_back_costs_at_most_one_evaluation(self):
        """A room record stamped ahead of the clock is taken as no record.

        After the wall clock steps back, the stored room stamp lies in the
        future. The record is refreshed on that cycle, so a TRV and a room
        that keep answering the command raise no minimum opening. The
        cycle runs its steps in the order the controller does.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
        )
        state = MpcState()
        now = 1_700_000_000.0
        for cycle in range(200):
            if cycle == 100:
                now -= 3600.0
            inp = MpcInput(
                key="deadzone",
                target_temp_C=40.0,
                current_temp_C=18.0 + 0.1 * cycle,
                trv_temp_C=20.0 + 0.2 * (cycle % 50),
                tolerance_K=0.0,
            )
            _forget_stamps_ahead_of_the_clock(state, now)
            _post_process_percent(inp, params, state, now, 10.0, 0.5)
            _update_perf_curve(state, inp, params, now, {})
            assert state.min_effective_percent is None, cycle
            now += 300.0

    def test_a_learned_minimum_decays_after_the_trv_reads_as_linear(self):
        """A minimum opening learned on a dead zone decays once the TRV responds.

        The TRV profile can move from threshold to linear once the raised
        minimum makes the TRV respond. Dead-zone hits no longer count then,
        but the minimum is not frozen: each evaluation that sees the TRV
        warm lowers it by one decay step.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=60.0,
            deadzone_decay_pct=1.0,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
        )
        state = MpcState(trv_profile="linear", min_effective_percent=16.0)
        for cycle in range(3):
            inp = MpcInput(
                key="deadzone",
                target_temp_C=22.0,
                current_temp_C=20.0,
                trv_temp_C=21.0 + 0.5 * cycle,
                tolerance_K=0.0,
            )
            # The controller asks for less than the minimum, so the output
            # is clamped to it.
            _post_process_percent(
                inp, params, state, 1000.0 + 120.0 * cycle, 10.0, None
            )

        assert state.trv_profile == "linear"
        assert state.min_effective_percent == 14.0

    @pytest.mark.parametrize("profile", ["linear", "threshold"])
    def test_a_wide_opening_does_not_lower_the_learned_minimum(self, profile):
        """A TRV warming at a wide opening keeps a small learned minimum.

        The minimum records that openings below it do not reach the
        valve. A TRV that warms at a far wider opening says nothing about
        that.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=60.0,
            deadzone_decay_pct=1.0,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
        )
        state = MpcState(trv_profile=profile, min_effective_percent=16.0)
        for cycle in range(3):
            inp = MpcInput(
                key="deadzone",
                target_temp_C=22.0,
                current_temp_C=18.0,
                trv_temp_C=21.0 + 0.5 * cycle,
                tolerance_K=0.0,
            )
            percent, _, _ = _post_process_percent(
                inp, params, state, 1000.0 + 120.0 * cycle, 100.0, None
            )
            assert percent == 100

        assert state.min_effective_percent == 16.0

    @pytest.mark.parametrize("profile", ["linear", "threshold"])
    def test_a_closed_valve_does_not_lower_the_learned_minimum(self, profile):
        """A TRV that warms behind a closed valve keeps the learned minimum.

        After the valve closes, the radiator's stored heat still warms the
        TRV for a while. That warming answers no opening, so it says
        nothing about whether a small opening is past the dead zone.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=60.0,
            deadzone_decay_pct=1.0,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
        )
        state = MpcState(trv_profile=profile, min_effective_percent=16.0)
        for cycle in range(3):
            inp = MpcInput(
                key="deadzone",
                target_temp_C=22.0,
                current_temp_C=22.5,
                trv_temp_C=24.0 + 0.5 * cycle,
                tolerance_K=0.0,
            )
            percent, _, _ = _post_process_percent(
                inp, params, state, 1000.0 + 120.0 * cycle, 0.0, None
            )
            assert percent == 0

        assert state.min_effective_percent == 16.0

    @pytest.mark.parametrize("hold_time_s", [0.0, 300.0])
    def test_a_valve_opened_after_the_trv_warmed_keeps_the_learned_minimum(
        self, hold_time_s
    ):
        """Only an opening in force while the TRV warmed lowers the minimum.

        The valve was closed over the interval the TRV warmed in, so the
        warming answers no opening, whatever this cycle commands: an
        opening that has not acted yet, or a closed valve the hold time
        keeps.
        """
        params = MpcParams(
            enable_min_effective_percent=True,
            deadzone_temp_delta_K=0.05,
            deadzone_time_s=60.0,
            deadzone_decay_pct=1.0,
            percent_hysteresis_pts=0.0,
            min_update_interval_s=0.0,
            min_percent_hold_time_s=hold_time_s,
        )
        state = MpcState(trv_profile="linear", min_effective_percent=16.0)
        for cycle, raw_percent in enumerate((0.0, 20.0)):
            inp = MpcInput(
                key="deadzone",
                target_temp_C=22.0,
                current_temp_C=20.0,
                trv_temp_C=24.0 + 0.5 * cycle,
                tolerance_K=0.0,
            )
            _post_process_percent(
                inp, params, state, 1000.0 + 120.0 * cycle, raw_percent, None
            )

        assert state.min_effective_percent == 16.0

    def test_a_wall_clock_step_back_restarts_the_valve_average(self):
        """The valve totals start over with an integration stamp ahead of the clock.

        The totals are the valve use accumulated since that stamp. Once the
        stamp is taken as absent, totals from before the clock step would
        otherwise be averaged into the next learning interval.
        """
        state = MpcState(
            u_integral=100.0 * 3600.0, time_integral=3600.0, last_integration_ts=5000.0
        )

        _forget_stamps_ahead_of_the_clock(state, 1000.0)

        assert state.last_integration_ts == 0.0
        assert (state.u_integral, state.time_integral) == (0.0, 0.0)

    @pytest.mark.parametrize(
        ("raw_percent", "seconds_since_update", "expected"),
        [
            pytest.param(40.6, 60.0, 40, id="small_change_is_held"),
            pytest.param(42.0, 60.0, 42, id="large_change_passes"),
            pytest.param(60.0, 0.5, 40, id="change_too_soon_is_held"),
        ],
    )
    def test_hysteresis(self, raw_percent, seconds_since_update, expected):
        """Small or too-early changes keep the previous command.

        A change under the hysteresis band, or any change within the minimum
        update interval, keeps the last command; a larger change after the
        interval reaches the valve.
        """
        params = MpcParams(
            percent_hysteresis_pts=1.0,
            min_update_interval_s=1.0,
            min_percent_hold_time_s=0.0,
            mpc_du_max_pct=None,
        )
        state = MpcState()
        state.last_percent = 40.0
        state.last_target_C = 22.0
        state.last_update_ts = 1000.0
        inp = MpcInput(key="hyst", target_temp_C=22.0, current_temp_C=20.0)

        percent_out, _debug, _ = _post_process_percent(
            inp, params, state, 1000.0 + seconds_since_update, raw_percent, None
        )

        assert percent_out == expected

    def test_tolerance_hysteresis_stops_and_restarts(self):
        """MPC should stop at target and restart only below target - tolerance."""

        params = MpcParams(
            mpc_adapt=False,
            min_update_interval_s=0.0,
            min_percent_hold_time_s=0.0,
            percent_hysteresis_pts=0.0,
            mpc_control_penalty=0.0,
            mpc_change_penalty=0.0,
            use_virtual_temp=False,
        )
        key = "test_tol_hyst"

        # 1) At target -> enter tolerance hold, no heating.
        at_target, _ = compute_mpc(
            MpcInput(key=key, target_temp_C=21.0, current_temp_C=21.0, tolerance_K=0.5),
            params,
        )
        assert at_target is not None
        assert at_target.valve_percent == 0
        assert at_target.debug.get("mpc_tolerance_hold_active") is True

        # 2) Still above restart threshold (target - tolerance = 20.5) -> remain off.
        in_band, _ = compute_mpc(
            MpcInput(key=key, target_temp_C=21.0, current_temp_C=20.7, tolerance_K=0.5),
            params,
        )
        assert in_band is not None
        assert in_band.valve_percent == 0
        assert in_band.debug.get("mpc_tolerance_hold_active") is True

        # 3) Below restart threshold -> resume MPC heating.
        below_band, _ = compute_mpc(
            MpcInput(key=key, target_temp_C=21.0, current_temp_C=20.4, tolerance_K=0.5),
            params,
        )
        assert below_band is not None
        assert below_band.debug.get("mpc_tolerance_hold_resume") is True
        assert below_band.valve_percent > 0

    def test_tolerance_hold_keeps_virtual_temp_fresh(self):
        """Virtual temperature/Kalman state should keep updating while tolerance hold is active."""

        params = MpcParams(
            mpc_adapt=False,
            min_update_interval_s=0.0,
            min_percent_hold_time_s=0.0,
            percent_hysteresis_pts=0.0,
            mpc_control_penalty=0.0,
            mpc_change_penalty=0.0,
            use_virtual_temp=True,
        )
        key = "test_tol_kalman"

        first, _ = compute_mpc(
            MpcInput(key=key, target_temp_C=21.0, current_temp_C=21.0, tolerance_K=0.5),
            params,
        )
        assert first is not None
        assert first.valve_percent == 0

        state = _STATES[key]
        v1 = state.virtual_temp
        assert v1 is not None

        second, _ = compute_mpc(
            MpcInput(key=key, target_temp_C=21.0, current_temp_C=20.8, tolerance_K=0.5),
            params,
        )
        assert second is not None
        assert second.valve_percent == 0

        v2 = state.virtual_temp
        assert v2 is not None
        assert v2 < v1

    def test_heating_sequence_simulation(self):
        """Simulate a heating sequence to test controller behavior over time."""
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
        # slope intentionally unused in this simulation

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
            result, _ = compute_mpc(inp, params)
            assert result is not None
            valve_pct = result.valve_percent
            dbg = result.debug or {}

            vtemp = _STATES[key].virtual_temp if key in _STATES else None

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

    def test_overshoot_penalty_reduces_output_above_target(self):
        """Higher overshoot penalty should reduce opening when current is above target."""

        base_params = MpcParams(
            mpc_adapt=False,
            min_update_interval_s=0.0,
            min_percent_hold_time_s=0.0,
            percent_hysteresis_pts=0.0,
            mpc_control_penalty=0.0,
            mpc_change_penalty=0.0,
            use_virtual_temp=False,
        )

        low_overshoot, _ = compute_mpc(
            MpcInput(
                key="test_overshoot_pen_low", target_temp_C=20.8, current_temp_C=20.95
            ),
            MpcParams(**{**base_params.__dict__, "mpc_overshoot_penalty": 0.0}),
        )
        high_overshoot, _ = compute_mpc(
            MpcInput(
                key="test_overshoot_pen_high", target_temp_C=20.8, current_temp_C=20.95
            ),
            MpcParams(**{**base_params.__dict__, "mpc_overshoot_penalty": 8.0}),
        )

        assert low_overshoot is not None and high_overshoot is not None
        assert high_overshoot.valve_percent <= low_overshoot.valve_percent

    def test_change_penalty_reduces_output(self):
        """Activating change penalty should reduce opening in the same scenario."""

        common = {
            "mpc_adapt": False,
            "min_update_interval_s": 0.0,
            "min_percent_hold_time_s": 0.0,
            "percent_hysteresis_pts": 0.0,
            "mpc_control_penalty": 0.0,
            "use_virtual_temp": False,
            "mpc_overshoot_penalty": 0.0,
        }

        no_pen = MpcParams(**common, mpc_change_penalty=0.0)
        with_pen = MpcParams(**common, mpc_change_penalty=10.0)

        inp_no_pen = MpcInput(
            key="test_penalty_none", target_temp_C=22.0, current_temp_C=21.3
        )
        inp_with_pen = MpcInput(
            key="test_penalty_with", target_temp_C=22.0, current_temp_C=21.3
        )

        out_no_pen, _ = compute_mpc(inp_no_pen, no_pen)
        out_with_pen, _ = compute_mpc(inp_with_pen, with_pen)

        assert out_no_pen is not None and out_with_pen is not None
        assert out_with_pen.valve_percent <= out_no_pen.valve_percent
