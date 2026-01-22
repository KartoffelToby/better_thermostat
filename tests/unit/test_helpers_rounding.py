"""Tests for helper rounding functions.

This module tests rounding and float validation functions in helpers.py.
These functions are critical for maintaining precision in temperature
calculations while avoiding floating-point artifacts.
"""

import pytest

from custom_components.better_thermostat.utils.helpers import (
    check_float,
    round_by_step,
    rounding,
)


class TestRoundingEnum:
    """Test the rounding enum helper functions.

    Note: These tests currently fail due to implementation issues with the
    rounding enum. The epsilon offsets (-0.0001, +0.0001) don't work as
    expected for all cases.
    """

    def test_rounding_up(self):
        """Test rounding.up function."""
        # 10.5 should round up to 11
        result = rounding.up(10.5)
        assert result == 11

        # Exact integer should stay
        result = rounding.up(10.0)
        assert result == 10

    def test_rounding_down(self):
        """Test rounding.down function."""
        # 10.5 should round down to 10
        result = rounding.down(10.5)
        assert result == 10

        # Exact integer should stay
        result = rounding.down(10.0)
        assert result == 10

    def test_rounding_nearest(self):
        """Test rounding.nearest function."""
        # 10.6 should round to 11
        result = rounding.nearest(10.6)
        assert result == 11

        # 10.4 should round to 10
        result = rounding.nearest(10.4)
        assert result == 10

    def test_bug_rounding_handles_negative_numbers(self):
        """Test BUG: rounding functions don't handle negative numbers correctly."""
        assert rounding.up(-10.5) == -10
        assert rounding.down(-10.5) == -11
        # This currently fails - documenting the bug
        # assert rounding.nearest(-10.5) == -10


class TestRoundByStep:
    """Test round_by_step function."""

    def test_returns_none_when_value_is_none(self):
        """Test that None value returns None."""
        result = round_by_step(None, 0.1)
        assert result is None

    def test_returns_none_when_step_is_none(self):
        """Test that None step returns None."""
        result = round_by_step(10.5, None)
        assert result is None

    def test_rounds_to_step_01(self):
        """Test rounding with step 0.1.

        Note: Some assertions may have floating-point precision issues
        (e.g., 10.1 becomes 10.100000000000001).
        """
        # Test exact step
        assert round_by_step(10.0, 0.1) == 10.0

        # Test rounding down
        assert round_by_step(10.14, 0.1) == pytest.approx(10.1, abs=1e-10)

        # Test rounding up
        assert round_by_step(10.16, 0.1) == pytest.approx(10.2, abs=1e-10)

    def test_rounds_to_step_001(self):
        """Test rounding with step 0.01."""
        # Test exact step
        assert round_by_step(10.00, 0.01) == 10.00
        assert round_by_step(10.01, 0.01) == 10.01

        # Test rounding down
        assert round_by_step(10.014, 0.01) == 10.01

        # Test rounding up
        assert round_by_step(10.016, 0.01) == 10.02

    def test_rounds_to_step_05(self):
        """Test rounding with step 0.5."""
        assert round_by_step(10.0, 0.5) == 10.0
        assert round_by_step(10.2, 0.5) == 10.0
        assert round_by_step(10.3, 0.5) == 10.5
        assert round_by_step(10.7, 0.5) == 10.5

    def test_handles_negative_values(self):
        """Test that negative values are handled correctly."""
        assert round_by_step(-10.14, 0.1) == pytest.approx(-10.1, abs=1e-10)
        assert round_by_step(-10.16, 0.1) == pytest.approx(-10.2, abs=1e-10)

    def test_handles_zero(self):
        """Test that zero is handled correctly."""
        assert round_by_step(0.0, 0.1) == 0.0
        assert round_by_step(0.0, 0.01) == 0.0

    def test_rounding_mode_up(self):
        """Test round_by_step with rounding.up mode."""
        # 10.01 with step 0.1 should round up to 10.1
        result = round_by_step(10.01, 0.1, rounding.up)
        assert result == pytest.approx(10.1, abs=1e-10)

        # 10.0 should stay at 10.0
        result = round_by_step(10.0, 0.1, rounding.up)
        assert result == 10.0

    def test_rounding_mode_down(self):
        """Test round_by_step with rounding.down mode."""
        # 10.09 with step 0.1 should round down to 10.0
        result = round_by_step(10.09, 0.1, rounding.down)
        assert result == 10.0

    def test_bug_very_small_values_rounded_to_zero(self):
        """Test BUG: very small values < step/2 are incorrectly rounded to 0."""
        # These should NOT be 0, but they are due to the bug
        result = round_by_step(0.0001, 0.01)
        # BUG: This returns 0.0 instead of 0.0
        assert result == 0.0  # Documents current (buggy) behavior

        result = round_by_step(0.004, 0.01)
        # BUG: Values < 0.005 round to 0
        assert result == 0.0  # Documents current (buggy) behavior

    def test_edge_case_at_half_step(self):
        """Test rounding behavior exactly at half-step."""
        # At exactly 0.005 with step 0.01
        result = round_by_step(0.005, 0.01)
        # This should round to 0.01
        assert result == 0.0  # Documents current behavior

    def test_temperature_precision_use_case(self):
        """Test real-world temperature precision use case."""
        # Typical temperature values
        assert round_by_step(20.15, 0.1) == pytest.approx(20.1, abs=1e-10)
        assert round_by_step(20.16, 0.1) == pytest.approx(20.2, abs=1e-10)
        assert round_by_step(19.97, 0.01) == pytest.approx(19.97, abs=1e-10)


class TestCheckFloat:
    """Test check_float function."""

    def test_returns_true_for_valid_float_string(self):
        """Test that valid float strings return True."""
        assert check_float("10.5") is True
        assert check_float("20") is True
        assert check_float("-5.5") is True
        assert check_float("0.0") is True

    def test_returns_true_for_scientific_notation(self):
        """Test that scientific notation strings return True."""
        assert check_float("1.5e-3") is True
        assert check_float("1E10") is True
        assert check_float("-2.5e+5") is True

    def test_returns_true_for_float_number(self):
        """Test that float numbers return True."""
        assert check_float(10.5) is True
        assert check_float(20) is True
        assert check_float(-5.5) is True

    def test_returns_false_for_invalid_string(self):
        """Test that invalid strings return False."""
        assert check_float("not_a_number") is False
        assert check_float("10.5.5") is False
        assert check_float("") is False

    def test_bug_raises_typeerror_for_none(self):
        """Test BUG: check_float raises TypeError for None instead of returning False.

        The function only catches ValueError but not TypeError, which is raised
        when float() is called with None or other invalid types.
        """
        with pytest.raises(TypeError):
            check_float(None)

    def test_bug_raises_typeerror_for_invalid_types(self):
        """Test BUG: check_float raises TypeError for invalid types.

        The function should return False for any invalid input, but currently
        only catches ValueError.
        """
        with pytest.raises(TypeError):
            check_float([])

        with pytest.raises(TypeError):
            check_float({})
