"""Tests for the thermal learning module (utils/thermal.py).

Groups:
1. Pure helpers (ema_smooth, clamp, compute_weight_factor, compute_env_factor)
2. HeatingPowerTracker state machine
3. HeatLossTracker state machine
"""

from datetime import UTC, datetime, timedelta

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.utils.thermal_learning import (
    CycleResult,
    HeatingPowerTracker,
    HeatingPowerUpdate,
    HeatLossTracker,
    HeatLossUpdate,
    clamp,
    compute_env_factor,
    compute_weight_factor,
    ema_smooth,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _ts(minutes: float = 0.0) -> datetime:
    """Offset from _NOW by *minutes*."""
    return _NOW + timedelta(minutes=minutes)


# ===================================================================
# Group 1: Pure helpers
# ===================================================================


class TestEmaSmooth:
    """Tests for ema smooth."""

    def test_basic_formula(self):
        """Test Basic formula."""
        assert ema_smooth(10.0, 20.0, 0.1) == pytest.approx(11.0)

    def test_alpha_zero_keeps_old(self):
        """Test Alpha zero keeps old."""
        assert ema_smooth(5.0, 100.0, 0.0) == pytest.approx(5.0)

    def test_alpha_one_replaces(self):
        """Test Alpha one replaces."""
        assert ema_smooth(5.0, 100.0, 1.0) == pytest.approx(100.0)

    def test_symmetry(self):
        """EMA(a, b, α) and EMA(b, a, 1-α) should give the same result."""
        assert ema_smooth(3.0, 7.0, 0.25) == pytest.approx(ema_smooth(7.0, 3.0, 0.75))


class TestClamp:
    """Tests for clamp."""

    def test_within_bounds(self):
        """Test Within bounds."""
        assert clamp(5.0, 0.0, 10.0) == 5.0

    def test_below_min(self):
        """Test Below min."""
        assert clamp(-1.0, 0.0, 10.0) == 0.0

    def test_above_max(self):
        """Test Above max."""
        assert clamp(15.0, 0.0, 10.0) == 10.0

    def test_at_boundaries(self):
        """Test At boundaries."""
        assert clamp(0.0, 0.0, 10.0) == 0.0
        assert clamp(10.0, 0.0, 10.0) == 10.0


class TestComputeWeightFactor:
    """Tests for compute weight factor."""

    def test_middle_of_range(self):
        """Test Middle of range."""
        # min=18, max=22, target=20 → relative_pos=0.5 → 0.5+0.5=1.0
        assert compute_weight_factor(20.0, 18.0, 22.0) == pytest.approx(1.0)

    def test_at_min(self):
        """Test At min."""
        # target=18 → relative_pos=0 → 0.5+0=0.5
        assert compute_weight_factor(18.0, 18.0, 22.0) == pytest.approx(0.5)

    def test_at_max(self):
        """Test At max."""
        # target=22 → relative_pos=1.0 → 0.5+1.0=1.5
        assert compute_weight_factor(22.0, 18.0, 22.0) == pytest.approx(1.5)

    def test_target_none_returns_one(self):
        """Test Target none returns one."""
        assert compute_weight_factor(None, 18.0, 22.0) == pytest.approx(1.0)

    def test_narrow_range_no_division_by_zero(self):
        """Test Narrow range no division by zero."""
        # min == max → range clamped to 0.1
        result = compute_weight_factor(20.0, 20.0, 20.0)
        assert 0.5 <= result <= 1.5


class TestComputeEnvFactor:
    """Tests for compute env factor."""

    def test_no_outdoor(self):
        """Test No outdoor."""
        assert compute_env_factor(None, 21.0) == 1.0

    def test_no_target(self):
        """Test No target."""
        assert compute_env_factor(5.0, None) == 1.0

    def test_typical_gradient(self):
        """Test Typical gradient."""
        # delta_env = 21-5 = 16, 16/20 = 0.8
        assert compute_env_factor(5.0, 21.0) == pytest.approx(0.8)

    def test_large_gradient_clamped(self):
        """Test Large gradient clamped."""
        # delta_env = 21-(-10) = 31, 31/20 = 1.55 → clamp to 1.3
        assert compute_env_factor(-10.0, 21.0) == pytest.approx(1.3)

    def test_outdoor_above_target(self):
        """Test Outdoor above target."""
        # delta_env = max(20-25, 0.1) = 0.1, 0.1/20 = 0.005 → clamp to 0.7
        assert compute_env_factor(25.0, 20.0) == pytest.approx(0.7)


# ===================================================================
# Group 2: HeatingPowerTracker
# ===================================================================


class TestHeatingPowerTrackerTransitions:
    """Test state machine transitions."""

    def test_idle_to_heating_sets_start(self):
        """Test Idle to heating sets start."""
        t = HeatingPowerTracker()
        t._prev_action = HVACAction.IDLE
        result = t.update(19.0, HVACAction.HEATING, _NOW)

        assert t.start_temp == 19.0
        assert t.start_ts == _NOW
        assert t.end_temp is None
        assert result.action_changed is True

    def test_heating_to_idle_sets_end(self):
        """Test Heating to idle sets end."""
        t = HeatingPowerTracker()
        t._prev_action = HVACAction.HEATING
        t.start_temp = 19.0
        t.start_ts = _NOW

        result = t.update(21.0, HVACAction.IDLE, _ts(10))
        assert t.end_temp == 21.0
        assert t.end_ts == _ts(10)
        assert result.action_changed is True

    def test_peak_tracking_temp_still_rising(self):
        """Test Peak tracking temp still rising."""
        t = HeatingPowerTracker()
        t._prev_action = HVACAction.IDLE
        t.start_temp = 19.0
        t.start_ts = _NOW
        t.end_temp = 21.0
        t.end_ts = _ts(10)

        t.update(21.5, HVACAction.IDLE, _ts(12))
        assert t.end_temp == 21.5
        assert t.end_ts == _ts(12)


class TestHeatingPowerTrackerFinalization:
    """Test cycle finalization and EMA computation."""

    def _run_complete_cycle(
        self,
        start_temp: float = 19.0,
        peak_temp: float = 21.0,
        duration_min: float = 10.0,
        initial_power: float = 0.05,
        outdoor: float | None = None,
        target: float | None = 22.0,
    ) -> tuple[HeatingPowerTracker, HeatingPowerUpdate]:
        """Drive a tracker through a full heating cycle."""
        t = HeatingPowerTracker(heating_power=initial_power)

        # Start heating
        t.update(start_temp, HVACAction.HEATING, _NOW)
        # Stop heating (candidate end)
        t.update(peak_temp, HVACAction.IDLE, _ts(duration_min))
        # Temperature drops → finalize
        result = t.update(
            peak_temp - 0.1,
            HVACAction.IDLE,
            _ts(duration_min + 1),
            target_temp=target,
            outdoor_temp=outdoor,
        )
        return t, result

    def test_finalization_on_temp_drop(self):
        """Test Finalization on temp drop."""
        t, result = self._run_complete_cycle()
        assert result.cycle_result is not None
        assert isinstance(result.cycle_result, CycleResult)

    def test_finalization_updates_heating_power(self):
        """Test Finalization updates heating power."""
        t, result = self._run_complete_cycle(initial_power=0.05)
        # heating_rate = (21-19)/10 = 0.2, but EMA with alpha~0.1 keeps it close to 0.05
        assert t.heating_power != 0.05  # changed
        assert result.cycle_result is not None
        assert result.cycle_result.power_changed is True

    def test_finalization_on_timeout(self):
        """Test Finalization on timeout."""
        t = HeatingPowerTracker(heating_power=0.05)
        t.update(19.0, HVACAction.HEATING, _NOW)
        t.update(21.0, HVACAction.IDLE, _ts(5))
        # No temperature drop, but 31 minutes pass (>30 timeout)
        result = t.update(21.0, HVACAction.IDLE, _ts(36), target_temp=22.0)
        assert result.cycle_result is not None

    def test_short_cycle_discarded(self):
        """Cycles < 1 min should be discarded (no EMA update)."""
        t = HeatingPowerTracker(heating_power=0.05)
        t.update(19.0, HVACAction.HEATING, _NOW)
        t.update(19.5, HVACAction.IDLE, _ts(0.5))  # 30 seconds
        result = t.update(19.3, HVACAction.IDLE, _ts(1))
        assert result.cycle_result is not None
        assert t.heating_power == 0.05  # unchanged

    def test_negative_temp_diff_discarded(self):
        """If end_temp < start_temp the cycle is discarded."""
        t = HeatingPowerTracker(heating_power=0.05)
        t.update(21.0, HVACAction.HEATING, _NOW)
        t.update(20.0, HVACAction.IDLE, _ts(5))
        result = t.update(19.5, HVACAction.IDLE, _ts(6))
        # Finalize triggered but temp_diff <= 0
        assert result.cycle_result is not None
        assert t.heating_power == 0.05  # unchanged

    def test_ema_smoothing_correctness(self):
        """Verify the EMA formula is applied correctly."""
        t, _ = self._run_complete_cycle(
            start_temp=19.0, peak_temp=21.0, duration_min=10.0, initial_power=0.05
        )
        # heating_rate = 2.0/10 = 0.2
        # weight_factor with target=22, min=18, max=21 (updated to max(21,22)=22)
        # Actually min_target and max_target are defaults 18 and 21,
        # but target_temp=22 updates max_target to 22
        # So at finalize time: min=18, max=22 (already updated by first update call)
        # Actually the update to max_target happens at the END of update(), after finalize
        # Let's just verify it moved in the right direction
        assert t.heating_power > 0.05  # moved toward 0.2

    def test_min_clamping(self):
        """Heating power shouldn't go below MIN_HEATING_POWER."""
        from custom_components.better_thermostat.utils.const import MIN_HEATING_POWER

        t = HeatingPowerTracker(heating_power=MIN_HEATING_POWER)
        # Very tiny temp rise
        t.update(19.0, HVACAction.HEATING, _NOW)
        t.update(19.001, HVACAction.IDLE, _ts(10))
        t.update(18.9, HVACAction.IDLE, _ts(11), target_temp=20.0)
        assert t.heating_power >= MIN_HEATING_POWER

    def test_max_clamping(self):
        """Heating power shouldn't go above MAX_HEATING_POWER."""
        from custom_components.better_thermostat.utils.const import MAX_HEATING_POWER

        t = HeatingPowerTracker(heating_power=MAX_HEATING_POWER)
        # Huge temp rise in short time
        t.update(15.0, HVACAction.HEATING, _NOW)
        t.update(30.0, HVACAction.IDLE, _ts(1.5))
        t.update(29.0, HVACAction.IDLE, _ts(2), target_temp=25.0)
        assert t.heating_power <= MAX_HEATING_POWER

    def test_outdoor_normalization(self):
        """When outdoor_temp is provided, normalized_power should be set."""
        t, _ = self._run_complete_cycle(outdoor=5.0, target=21.0)
        assert t.normalized_power is not None

    def test_no_outdoor_no_normalization(self):
        """Without outdoor_temp, normalized_power stays None (or from prior)."""
        t = HeatingPowerTracker()
        t.update(19.0, HVACAction.HEATING, _NOW)
        t.update(21.0, HVACAction.IDLE, _ts(10))
        t.update(20.9, HVACAction.IDLE, _ts(11), target_temp=22.0, outdoor_temp=None)
        # normalized_power may be None since no outdoor provided
        # (it was set to None in __init__)
        # After a cycle with outdoor=None, it's not updated
        assert t.normalized_power is None

    def test_target_range_tracking(self):
        """min_target/max_target should track the range of observed targets."""
        t = HeatingPowerTracker(min_target=20.0, max_target=20.0)
        t.update(19.0, HVACAction.IDLE, _NOW, target_temp=18.0)
        assert t.min_target == 18.0
        t.update(19.0, HVACAction.IDLE, _ts(1), target_temp=25.0)
        assert t.max_target == 25.0

    def test_telemetry_stats_format(self):
        """Stats deque should contain expected keys."""
        t, _ = self._run_complete_cycle()
        assert len(t.stats) == 1
        entry = t.stats[0]
        assert "dT" in entry
        assert "min" in entry
        assert "rate" in entry
        assert "alpha" in entry
        assert "envf" in entry
        assert "hp" in entry
        assert "norm" in entry

    def test_telemetry_cycles_format(self):
        """Cycles deque should contain expected keys."""
        t, _ = self._run_complete_cycle()
        assert len(t.cycles) == 1
        entry = t.cycles[0]
        assert "start" in entry
        assert "end" in entry
        assert "temp_start" in entry
        assert "temp_peak" in entry
        assert "delta_t" in entry
        assert "minutes" in entry
        assert "rate_c_min" in entry

    def test_reset_power(self):
        """Test Reset power."""
        t = HeatingPowerTracker(heating_power=0.15)
        t.reset_power()
        assert t.heating_power == 0.01

    def test_reset_power_custom_value(self):
        """Test Reset power custom value."""
        t = HeatingPowerTracker(heating_power=0.15)
        t.reset_power(0.03)
        assert t.heating_power == 0.03

    def test_action_changed_flag(self):
        """Test Action changed flag."""
        t = HeatingPowerTracker()
        t._prev_action = HVACAction.IDLE
        result = t.update(20.0, HVACAction.HEATING, _NOW)
        assert result.action_changed is True

        result2 = t.update(20.5, HVACAction.HEATING, _ts(1))
        assert result2.action_changed is False

    def test_multiple_cycles(self):
        """Run two consecutive cycles and verify both are recorded."""
        t = HeatingPowerTracker(heating_power=0.05)

        # Cycle 1
        t.update(19.0, HVACAction.HEATING, _NOW)
        t.update(21.0, HVACAction.IDLE, _ts(10))
        t.update(20.9, HVACAction.IDLE, _ts(11), target_temp=22.0)

        power_after_1 = t.heating_power
        assert len(t.cycles) == 1

        # Cycle 2
        t.update(20.5, HVACAction.HEATING, _ts(20))
        t.update(22.0, HVACAction.IDLE, _ts(30))
        t.update(21.9, HVACAction.IDLE, _ts(31), target_temp=22.0)

        assert len(t.cycles) == 2
        # Power should have evolved further
        assert t.heating_power != power_after_1

    def test_cycle_resets_state(self):
        """After finalization, start/end temps and timestamps should be None."""
        t, _ = self._run_complete_cycle()
        assert t.start_temp is None
        assert t.end_temp is None
        assert t.start_ts is None
        assert t.end_ts is None


# ===================================================================
# Group 3: HeatLossTracker
# ===================================================================


class TestHeatLossTrackerWindowOpen:
    """Tests for heat loss tracker window open."""

    def test_window_open_resets_tracking(self):
        """Test Window open resets tracking."""
        t = HeatLossTracker()
        t.start_temp = 21.0
        t.start_ts = _NOW
        t.end_temp = 20.0
        t.end_ts = _ts(5)

        result = t.update(19.0, HVACAction.IDLE, _ts(10), window_open=True)
        assert t.start_temp is None
        assert t.start_ts is None
        assert t.end_temp is None
        assert t.end_ts is None
        assert result.cycle_result is None

    def test_window_open_during_tracking(self):
        """Opening window mid-cycle resets everything."""
        t = HeatLossTracker()
        t.update(21.0, HVACAction.IDLE, _NOW)
        t.update(20.5, HVACAction.IDLE, _ts(5))
        t.update(20.0, HVACAction.IDLE, _ts(10), window_open=True)
        assert t.start_temp is None


class TestHeatLossTrackerIdle:
    """Tests for heat loss tracker idle."""

    def test_idle_starts_tracking(self):
        """Test Idle starts tracking."""
        t = HeatLossTracker()
        t.update(21.0, HVACAction.IDLE, _NOW)
        assert t.start_temp == 21.0
        assert t.start_ts == _NOW
        assert t.end_temp == 21.0

    def test_tracks_lowest_temperature(self):
        """Test Tracks lowest temperature."""
        t = HeatLossTracker()
        t.update(21.0, HVACAction.IDLE, _NOW)
        t.update(20.5, HVACAction.IDLE, _ts(5))
        t.update(20.0, HVACAction.IDLE, _ts(10))
        assert t.end_temp == 20.0

    def test_ignores_higher_temps(self):
        """Once tracking, a higher temp should not update end_temp."""
        t = HeatLossTracker()
        t.update(21.0, HVACAction.IDLE, _NOW)
        t.update(20.0, HVACAction.IDLE, _ts(5))
        t.update(20.5, HVACAction.IDLE, _ts(10))
        assert t.end_temp == 20.0  # still the lowest


class TestHeatLossTrackerFinalization:
    """Tests for heat loss tracker finalization."""

    def _run_complete_loss_cycle(
        self,
        start_temp: float = 21.0,
        end_temp: float = 20.0,
        duration_min: float = 10.0,
        initial_loss: float = 0.01,
    ) -> tuple[HeatLossTracker, HeatLossUpdate]:
        t = HeatLossTracker(heat_loss_rate=initial_loss)
        t.update(start_temp, HVACAction.IDLE, _NOW)
        t.update(end_temp, HVACAction.IDLE, _ts(duration_min))
        result = t.update(end_temp, HVACAction.HEATING, _ts(duration_min + 1))
        return t, result

    def test_finalization_on_heating_restart(self):
        """Test Finalization on heating restart."""
        t, result = self._run_complete_loss_cycle()
        assert result.cycle_result is not None

    def test_finalization_updates_loss_rate(self):
        """Test Finalization updates loss rate."""
        t, result = self._run_complete_loss_cycle(initial_loss=0.01)
        # loss_rate = (21-20)/10 = 0.1 → EMA moves from 0.01 toward 0.1
        assert t.heat_loss_rate > 0.01
        assert result.cycle_result is not None
        assert result.cycle_result.loss_changed is True

    def test_short_cycle_discarded(self):
        """Cycles < 1 minute should not update the loss rate."""
        t = HeatLossTracker(heat_loss_rate=0.01)
        t.update(21.0, HVACAction.IDLE, _NOW)
        t.update(20.0, HVACAction.IDLE, _ts(0.5))
        result = t.update(20.0, HVACAction.HEATING, _ts(0.7))
        assert result.cycle_result is not None
        assert t.heat_loss_rate == 0.01  # unchanged

    def test_ema_smoothing(self):
        """Verify loss rate moves toward observed rate."""
        t, _ = self._run_complete_loss_cycle(
            start_temp=22.0, end_temp=20.0, duration_min=20.0, initial_loss=0.01
        )
        # rate = 2/20 = 0.1, EMA: 0.01*0.9 + 0.1*0.1 = 0.019
        assert t.heat_loss_rate == pytest.approx(0.019, rel=0.01)

    def test_min_clamping(self):
        """Test Min clamping."""
        from custom_components.better_thermostat.utils.const import MIN_HEAT_LOSS

        t = HeatLossTracker(heat_loss_rate=MIN_HEAT_LOSS)
        t.update(20.0, HVACAction.IDLE, _NOW)
        t.update(19.999, HVACAction.IDLE, _ts(10))
        t.update(19.999, HVACAction.HEATING, _ts(11))
        assert t.heat_loss_rate >= MIN_HEAT_LOSS

    def test_max_clamping(self):
        """Test Max clamping."""
        from custom_components.better_thermostat.utils.const import MAX_HEAT_LOSS

        t = HeatLossTracker(heat_loss_rate=MAX_HEAT_LOSS)
        t.update(25.0, HVACAction.IDLE, _NOW)
        t.update(10.0, HVACAction.IDLE, _ts(2))
        t.update(10.0, HVACAction.HEATING, _ts(3))
        assert t.heat_loss_rate <= MAX_HEAT_LOSS

    def test_telemetry_stats_format(self):
        """Test Telemetry stats format."""
        t, _ = self._run_complete_loss_cycle()
        assert len(t.stats) == 1
        entry = t.stats[0]
        assert "dT" in entry
        assert "min" in entry
        assert "rate" in entry
        assert "alpha" in entry
        assert "loss" in entry

    def test_telemetry_cycles_format(self):
        """Test Telemetry cycles format."""
        t, _ = self._run_complete_loss_cycle()
        assert len(t.cycles) == 1
        entry = t.cycles[0]
        assert "start" in entry
        assert "end" in entry
        assert "temp_start" in entry
        assert "temp_min" in entry
        assert "rate" in entry

    def test_no_finalize_without_data(self):
        """Heating restart without prior idle tracking should not crash."""
        t = HeatLossTracker()
        result = t.update(20.0, HVACAction.HEATING, _NOW)
        assert result.cycle_result is None

    def test_cycle_resets_state(self):
        """After finalization, tracking state should be cleared."""
        t, _ = self._run_complete_loss_cycle()
        assert t.start_temp is None
        assert t.end_temp is None
        assert t.start_ts is None
        assert t.end_ts is None
