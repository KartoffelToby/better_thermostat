"""Tests for heating_power_valve_position calculation.

This module tests the heating power valve position calculation which uses
a heuristic formula to map temperature difference and heating power to
an expected valve opening percentage.
"""

import pytest

from custom_components.better_thermostat.utils.helpers import (
    heating_power_valve_position,
)


class MockThermostat:
    """Mock Better Thermostat instance for testing."""

    def __init__(
        self, bt_target_temp=20.0, cur_temp=18.0, heating_power=0.02, device_name="Test"
    ):
        """Initialize mock thermostat."""
        self.bt_target_temp = bt_target_temp
        self.cur_temp = cur_temp
        self.heating_power = heating_power
        self.device_name = device_name


class TestHeatingPowerValvePosition:
    """Test heating_power_valve_position function."""

    def test_returns_zero_when_target_equals_current(self):
        """Test that valve position is 0 when target temp equals current temp."""
        mock_bt = MockThermostat(bt_target_temp=20.0, cur_temp=20.0)
        result = heating_power_valve_position(mock_bt, "climate.test")

        # When temp_diff is 0, formula gives 0
        assert result == 0.0

    def test_returns_value_between_0_and_1(self):
        """Test that valve position is always between 0 and 1."""
        # Normal case
        mock_bt = MockThermostat(bt_target_temp=22.0, cur_temp=20.0, heating_power=0.02)
        result = heating_power_valve_position(mock_bt, "climate.test")
        assert 0.0 <= result <= 1.0

        # Large temp difference
        mock_bt = MockThermostat(bt_target_temp=25.0, cur_temp=15.0, heating_power=0.02)
        result = heating_power_valve_position(mock_bt, "climate.test")
        assert 0.0 <= result <= 1.0

    def test_higher_temp_diff_gives_higher_valve_position(self):
        """Test that larger temperature difference gives higher valve position."""
        mock_bt_small = MockThermostat(
            bt_target_temp=20.5, cur_temp=20.0, heating_power=0.02
        )
        mock_bt_large = MockThermostat(
            bt_target_temp=22.0, cur_temp=20.0, heating_power=0.02
        )

        result_small = heating_power_valve_position(mock_bt_small, "climate.test")
        result_large = heating_power_valve_position(mock_bt_large, "climate.test")

        assert result_large > result_small

    def test_lower_heating_power_gives_higher_valve_position(self):
        """Test that lower heating power (worse insulation) needs higher valve position."""
        # Better insulation (higher heating power value = less power needed)
        mock_bt_good_insulation = MockThermostat(
            bt_target_temp=22.0, cur_temp=20.0, heating_power=0.03
        )
        # Worse insulation (lower heating power value = more power needed)
        mock_bt_poor_insulation = MockThermostat(
            bt_target_temp=22.0, cur_temp=20.0, heating_power=0.01
        )

        result_good = heating_power_valve_position(
            mock_bt_good_insulation, "climate.test"
        )
        result_poor = heating_power_valve_position(
            mock_bt_poor_insulation, "climate.test"
        )

        # Poor insulation needs higher valve position
        # Note: Both should be clamped to same minimum valve opening
        assert result_poor >= result_good

    def test_clamps_heating_power_to_min_max(self):
        """Test that heating_power is clamped to MIN/MAX values."""
        # Very low heating power (should be clamped to MIN)
        mock_bt_too_low = MockThermostat(
            bt_target_temp=22.0, cur_temp=20.0, heating_power=0.0001
        )
        result_low = heating_power_valve_position(mock_bt_too_low, "climate.test")

        # Should be clamped to MIN_HEATING_POWER (0.001)
        # With MIN_HEATING_POWER, temp_diff=2.0 should give high valve position
        assert result_low > 0.5  # Should be fairly high

        # Very high heating power (should be clamped to MAX)
        mock_bt_too_high = MockThermostat(
            bt_target_temp=22.0, cur_temp=20.0, heating_power=0.5
        )
        result_high = heating_power_valve_position(mock_bt_too_high, "climate.test")

        # Should be clamped to MAX_HEATING_POWER (0.1)
        assert result_high < 0.5  # Should be lower than unclamped

    def test_applies_minimum_valve_opening_for_large_diff(self):
        """Test that minimum valve opening is applied for temp_diff > 1.0°C."""
        # VALVE_MIN_THRESHOLD_TEMP_DIFF = 1.0
        # VALVE_MIN_OPENING_LARGE_DIFF = 0.15
        mock_bt = MockThermostat(
            bt_target_temp=21.5, cur_temp=20.0, heating_power=0.05
        )
        result = heating_power_valve_position(mock_bt, "climate.test")

        # Should be at least VALVE_MIN_OPENING_LARGE_DIFF (15%)
        assert result >= 0.15

    def test_applies_proportional_minimum_for_small_diff(self):
        """Test proportional minimum for small temp differences (0.2-1.0°C)."""
        # VALVE_MIN_SMALL_DIFF_THRESHOLD = 0.2
        mock_bt = MockThermostat(
            bt_target_temp=20.5, cur_temp=20.0, heating_power=0.05
        )
        result = heating_power_valve_position(mock_bt, "climate.test")

        # Should have some minimum valve opening for 0.5°C diff
        assert result > 0.0

    def test_returns_zero_when_cooling_needed(self):
        """Test that valve returns 0% when cur_temp > target_temp."""
        mock_bt = MockThermostat(bt_target_temp=20.0, cur_temp=22.0)

        result = heating_power_valve_position(mock_bt, "climate.test")
        assert result == 0.0

    def test_returns_zero_for_negative_temp_diff(self):
        """Test that negative temperature differences return 0% valve."""
        mock_bt = MockThermostat(bt_target_temp=18.0, cur_temp=20.0, heating_power=0.02)

        result = heating_power_valve_position(mock_bt, "climate.test")
        assert result == 0.0

    def test_handles_very_small_temp_diff(self):
        """Test handling of very small temperature differences."""
        mock_bt = MockThermostat(
            bt_target_temp=20.05, cur_temp=20.0, heating_power=0.02
        )
        result = heating_power_valve_position(mock_bt, "climate.test")

        # Should be valid but small
        assert 0.0 <= result <= 0.3

    def test_formula_produces_expected_values(self):
        """Test that the formula produces expected valve positions for known inputs."""
        # From the comments in the code:
        # With heating_power of 0.02 and temp_diff of 0.5, expect ~0.3992
        mock_bt = MockThermostat(
            bt_target_temp=20.5, cur_temp=20.0, heating_power=0.02
        )
        result = heating_power_valve_position(mock_bt, "climate.test")

        # Allow some tolerance for float arithmetic and minimum valve logic
        assert 0.15 <= result <= 0.50

    def test_max_valve_position_for_large_difference(self):
        """Test that valve position reaches 100% for very large temp differences."""
        mock_bt = MockThermostat(
            bt_target_temp=25.0, cur_temp=15.0, heating_power=0.001
        )
        result = heating_power_valve_position(mock_bt, "climate.test")

        # With 10°C difference and low heating power, should be at or near 100%
        assert result >= 0.95

    def test_potential_division_by_zero(self):
        """Test behavior when heating_power is exactly 0."""
        mock_bt = MockThermostat(bt_target_temp=22.0, cur_temp=20.0, heating_power=0.0)

        # This should either handle gracefully or we've found a bug
        try:
            result = heating_power_valve_position(mock_bt, "climate.test")
            # If it doesn't crash, check the result is valid
            assert 0.0 <= result <= 1.0
        except (ZeroDivisionError, ValueError) as e:
            # If it crashes, we've found a bug!
            pytest.fail(f"BUG: Division by zero or ValueError: {e}")

    def test_potential_negative_heating_power(self):
        """Test behavior with negative heating_power."""
        mock_bt = MockThermostat(
            bt_target_temp=22.0, cur_temp=20.0, heating_power=-0.01
        )

        try:
            result = heating_power_valve_position(mock_bt, "climate.test")
            # Should be clamped to MIN_HEATING_POWER
            assert 0.0 <= result <= 1.0
        except (ValueError, ZeroDivisionError) as e:
            pytest.fail(f"BUG: Function doesn't handle negative heating_power: {e}")
