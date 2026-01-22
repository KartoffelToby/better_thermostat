"""Tests for check_float function.

This test file was extracted from the comprehensive helpers.py test suite
in PR #1868 to accompany the bug fix for TypeError handling.
"""

import pytest

from custom_components.better_thermostat.utils.helpers import check_float


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

    def test_returns_false_for_none(self):
        """Test that None returns False."""
        assert check_float(None) is False

    def test_returns_false_for_invalid_types(self):
        """Test that invalid types return False."""
        assert check_float([]) is False
        assert check_float({}) is False
