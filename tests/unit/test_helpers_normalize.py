"""Tests for helper normalization and conversion functions.

This module tests the normalization and conversion functions in helpers.py,
which are critical for handling user input and TRV state conversions.
"""

from homeassistant.components.climate.const import HVACMode

from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.helpers import (
    convert_to_float,
    is_calibration_mode,
    normalize_calibration_mode,
    normalize_hvac_mode,
)


class TestNormalizeCalibrationMode:
    """Test normalize_calibration_mode function."""

    def test_returns_enum_unchanged(self):
        """Test that enum values are returned unchanged."""
        result = normalize_calibration_mode(CalibrationMode.MPC_CALIBRATION)
        assert result == CalibrationMode.MPC_CALIBRATION
        assert isinstance(result, CalibrationMode)

    def test_converts_valid_string_to_enum(self):
        """Test that valid string values are converted to enum."""
        result = normalize_calibration_mode("mpc_calibration")
        assert result == CalibrationMode.MPC_CALIBRATION
        assert isinstance(result, CalibrationMode)

    def test_handles_string_with_whitespace(self):
        """Test that strings with whitespace are trimmed and converted."""
        result = normalize_calibration_mode("  pid_calibration  ")
        assert result == CalibrationMode.PID_CALIBRATION

    def test_handles_uppercase_string(self):
        """Test that uppercase strings are normalized to lowercase."""
        result = normalize_calibration_mode("TPI_CALIBRATION")
        assert result == CalibrationMode.TPI_CALIBRATION

    def test_returns_string_for_invalid_calibration_mode(self):
        """Test that invalid strings are returned as lowercase strings."""
        result = normalize_calibration_mode("custom_mode")
        assert result == "custom_mode"
        assert isinstance(result, str)

    def test_returns_none_for_none(self):
        """Test that None input returns None."""
        result = normalize_calibration_mode(None)
        assert result is None

    def test_returns_none_for_invalid_types(self):
        """Test that invalid types return None."""
        assert normalize_calibration_mode(123) is None
        assert normalize_calibration_mode([]) is None
        assert normalize_calibration_mode({}) is None


class TestIsCalibrationMode:
    """Test is_calibration_mode function."""

    def test_returns_true_for_matching_enum(self):
        """Test that matching enum returns True."""
        result = is_calibration_mode(
            CalibrationMode.MPC_CALIBRATION, CalibrationMode.MPC_CALIBRATION
        )
        assert result is True

    def test_returns_false_for_different_enum(self):
        """Test that different enum returns False."""
        result = is_calibration_mode(
            CalibrationMode.PID_CALIBRATION, CalibrationMode.MPC_CALIBRATION
        )
        assert result is False

    def test_returns_true_for_matching_string(self):
        """Test that matching string returns True."""
        result = is_calibration_mode("mpc_calibration", CalibrationMode.MPC_CALIBRATION)
        assert result is True

    def test_returns_false_for_different_string(self):
        """Test that different string returns False."""
        result = is_calibration_mode("pid_calibration", CalibrationMode.MPC_CALIBRATION)
        assert result is False

    def test_handles_string_with_whitespace(self):
        """Test that strings with whitespace are handled correctly."""
        result = is_calibration_mode(
            "  mpc_calibration  ", CalibrationMode.MPC_CALIBRATION
        )
        assert result is True

    def test_returns_false_for_none(self):
        """Test that None returns False."""
        result = is_calibration_mode(None, CalibrationMode.MPC_CALIBRATION)
        assert result is False

    def test_returns_false_for_custom_string(self):
        """Test that custom string mode returns False."""
        result = is_calibration_mode("custom_mode", CalibrationMode.MPC_CALIBRATION)
        assert result is False


class TestNormalizeHvacMode:
    """Test normalize_hvac_mode function."""

    def test_returns_enum_unchanged(self):
        """Test that HVACMode enum is returned unchanged."""
        result = normalize_hvac_mode(HVACMode.HEAT)
        assert result == HVACMode.HEAT
        assert isinstance(result, HVACMode)

    def test_converts_lowercase_string_to_enum(self):
        """Test that lowercase string is converted to enum."""
        result = normalize_hvac_mode("heat")
        assert result == HVACMode.HEAT

    def test_converts_uppercase_string_to_enum(self):
        """Test that uppercase string is converted to enum."""
        result = normalize_hvac_mode("HEAT")
        assert result == HVACMode.HEAT

    def test_strips_hvacmode_prefix(self):
        """Test that 'HVACMode.' prefix is stripped."""
        result = normalize_hvac_mode("HVACMode.HEAT")
        assert result == HVACMode.HEAT

    def test_strips_hvacmode_prefix_case_insensitive(self):
        """Test that prefix stripping is case-insensitive."""
        result = normalize_hvac_mode("hvacmode.heat")
        assert result == HVACMode.HEAT

    def test_handles_heat_cool_mode(self):
        """Test that heat_cool is converted correctly."""
        result = normalize_hvac_mode("heat_cool")
        assert result == HVACMode.HEAT_COOL

    def test_handles_all_standard_modes(self):
        """Test all standard HVAC modes."""
        modes = {
            "off": HVACMode.OFF,
            "heat": HVACMode.HEAT,
            "cool": HVACMode.COOL,
            "heat_cool": HVACMode.HEAT_COOL,
            "auto": HVACMode.AUTO,
            "dry": HVACMode.DRY,
            "fan_only": HVACMode.FAN_ONLY,
        }
        for string_mode, expected_enum in modes.items():
            assert normalize_hvac_mode(string_mode) == expected_enum

    def test_returns_string_for_unknown_mode(self):
        """Test that unknown modes are returned as lowercase strings."""
        result = normalize_hvac_mode("custom_mode")
        assert result == "custom_mode"
        assert isinstance(result, str)

    def test_handles_whitespace(self):
        """Test that whitespace is trimmed."""
        result = normalize_hvac_mode("  heat  ")
        assert result == HVACMode.HEAT


class TestConvertToFloat:
    """Test convert_to_float function."""

    def test_converts_float_unchanged(self):
        """Test that float values are returned unchanged."""
        result = convert_to_float(20.5, "test", "temperature")
        assert result == 20.5
        assert isinstance(result, float)

    def test_converts_int_to_float(self):
        """Test that int values are converted to float."""
        result = convert_to_float(20, "test", "temperature")
        assert result == 20.0
        assert isinstance(result, float)

    def test_converts_string_to_float(self):
        """Test that numeric strings are converted to float."""
        result = convert_to_float("20.5", "test", "temperature")
        assert result == 20.5

    def test_converts_string_with_whitespace(self):
        """Test that strings with whitespace are handled."""
        result = convert_to_float("  20.5  ", "test", "temperature")
        assert result == 20.5

    def test_returns_none_for_none(self):
        """Test that None input returns None."""
        result = convert_to_float(None, "test", "temperature")
        assert result is None

    def test_returns_none_for_invalid_string(self):
        """Test that invalid strings return None."""
        result = convert_to_float("not_a_number", "test", "temperature")
        assert result is None

    def test_returns_none_for_empty_string(self):
        """Test that empty strings return None."""
        result = convert_to_float("", "test", "temperature")
        assert result is None

    def test_handles_negative_numbers(self):
        """Test that negative numbers are handled correctly."""
        result = convert_to_float(-5.5, "test", "temperature")
        assert result == -5.5

    def test_handles_zero(self):
        """Test that zero is handled correctly."""
        result = convert_to_float(0, "test", "temperature")
        assert result == 0.0

    def test_rounds_very_small_numbers_to_zero(self):
        """Test BY DESIGN: very small numbers < 0.005 are rounded to 0.

        This is intentional behavior from PR #1805 which fixed issues #1792, #1789, #1785.
        The 0.01 step rounding (2 decimal precision) preserves sensor accuracy while
        preventing floating-point artifacts. Values < 0.005°C are too small to affect
        HVAC control decisions and are below typical sensor accuracy.

        Related: #1805 (fix), #1792 (original issue), test_temperature_precision.py
        """
        result = convert_to_float(0.0001, "test", "temperature")
        # Expected behavior: rounds to 0.0 due to 0.01 step
        assert result == 0.0

    def test_rounds_scientific_notation_to_zero(self):
        """Test BY DESIGN: scientific notation values < 0.005 are rounded to 0.

        This is intentional - temperature sensors typically don't provide
        precision below 0.01°C, so sub-0.005 values are negligible.
        """
        result = convert_to_float("1.5e-3", "test", "temperature")
        # Expected behavior: 0.0015 rounded to 0.0 due to 0.01 step
        assert result == 0.0
