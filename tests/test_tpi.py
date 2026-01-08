"""Tests for the TPI (Time Proportional Integrator) controller."""

from custom_components.better_thermostat.utils.calibration.tpi import (
    TpiInput,
    TpiParams,
    build_tpi_key,
    compute_tpi,
)


class TestTpiController:
    """Test cases for TPI controller."""

    def test_blocked_by_window_or_heating_not_allowed(self):
        """Test that duty cycle is 0 when heating is blocked."""
        params = TpiParams()
        inp = TpiInput(
            key="test",
            current_temp_C=20.0,
            target_temp_C=22.0,
            window_open=True,
            heating_allowed=True,
        )
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 0.0
        assert result.debug["reason"] == "blocked"

        inp.heating_allowed = False
        inp.window_open = False
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 0.0
        assert result.debug["reason"] == "blocked"

    def test_missing_temperatures(self):
        """Test behavior when temperatures are missing."""
        params = TpiParams()
        inp = TpiInput(key="test", current_temp_C=None, target_temp_C=22.0)
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 0.0  # No last_percent, so 0
        assert result.debug["reason"] == "missing_temps"

        # Now with last_percent
        inp.current_temp_C = 20.0
        result = compute_tpi(inp, params)
        # Should calculate normally, clamped to 100
        assert result.duty_cycle_pct == 100.0

    def test_normal_calculation(self):
        """Test normal TPI calculation."""
        params = TpiParams(coef_int=0.5, coef_ext=0.02)
        inp = TpiInput(
            key="test", current_temp_C=20.0, target_temp_C=22.0, outdoor_temp_C=15.0
        )
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 100.0  # clamped
        assert result.debug["error_K"] == 2.0
        assert result.debug["raw_pct"] == 114.0

    def test_overshoot_threshold(self):
        """Test that heating is disabled on overshoot."""
        params = TpiParams(threshold_high=0.5)
        inp = TpiInput(
            key="test",
            current_temp_C=22.6,
            target_temp_C=22.0,  # error = -0.6
        )
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 0.0
        assert result.debug["reason"] == "threshold_high"

    def test_clamping(self):
        """Test min/max clamping."""
        params = TpiParams(clamp_min_pct=10.0, clamp_max_pct=90.0, coef_int=1.0)
        inp = TpiInput(
            key="test",
            current_temp_C=20.0,
            target_temp_C=25.0,  # error=5, duty=500, clamped to 90
        )
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 90.0

        inp.target_temp_C = 19.0  # error=-1, duty=-100, clamped to 10
        result = compute_tpi(inp, params)
        assert result.duty_cycle_pct == 10.0

    def test_build_tpi_key(self):
        """Test key building for state tracking."""

        class MockBT:
            def __init__(self):
                self.bt_target_temp: float | None = 22.5
                self.unique_id = "test_bt"

        bt = MockBT()
        key = build_tpi_key(bt, "climate.test")
        assert key == "test_bt:climate.test:t22.5"

        bt.bt_target_temp = None
        key = build_tpi_key(bt, "climate.test")
        assert key == "test_bt:climate.test:tunknown"
