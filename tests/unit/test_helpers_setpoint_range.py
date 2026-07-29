"""Tests for the range-setpoint helpers in utils/helpers.py."""

from unittest.mock import Mock

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State

from custom_components.better_thermostat.utils.helpers import (
    SETPOINT_MATCH_TOLERANCE,
    celsius_to_system_temperature,
    get_current_set_temperatures,
    matches_any_setpoint,
    round_by_step,
    supports_temperature_range,
)

RANGE_BIT = int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)


def _fake_self():
    """Create a minimal BetterThermostat mock for attr_to_celsius."""
    mock_self = Mock()
    mock_self.device_name = "test_thermostat"
    mock_self.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    return mock_self


class TestSupportsTemperatureRange:
    """Feature detection reads the supported_features bitmask."""

    def test_none_state_returns_false(self):
        """Report no range support when the TRV state is missing."""
        assert supports_temperature_range(None) is False

    def test_missing_attribute_returns_false(self):
        """Report no range support when supported_features is absent."""
        state = State("climate.trv", "heat", {})
        assert supports_temperature_range(state) is False

    def test_bit_not_set_returns_false(self):
        """Report no range support when the range bit is not set."""
        state = State("climate.trv", "heat", {"supported_features": 0})
        assert supports_temperature_range(state) is False

    def test_bit_set_returns_true(self):
        """Report range support when the range bit is set."""
        state = State("climate.trv", "heat", {"supported_features": RANGE_BIT})
        assert supports_temperature_range(state) is True


class TestGetCurrentSetTemperatures:
    """Setpoint collection honors the range feature bit."""

    def test_single_setpoint_only(self):
        """Collect only the plain setpoint when the range feature is off."""
        state = State(
            "climate.trv", "heat", {"temperature": 20.0, "supported_features": 0}
        )
        assert get_current_set_temperatures(_fake_self(), state, "test") == {20.0}

    def test_range_low_included_when_supported(self):
        """Include target_temp_low when the range feature is active."""
        state = State(
            "climate.trv",
            "heat",
            {
                "temperature": 17.0,
                "target_temp_low": 21.0,
                "supported_features": RANGE_BIT,
            },
        )
        assert get_current_set_temperatures(_fake_self(), state, "test") == {17.0, 21.0}

    def test_range_low_ignored_without_feature_bit(self):
        """Ignore target_temp_low when the range feature bit is not set."""
        state = State(
            "climate.trv",
            "heat",
            {"temperature": 17.0, "target_temp_low": 21.0, "supported_features": 0},
        )
        assert get_current_set_temperatures(_fake_self(), state, "test") == {17.0}

    def test_none_state_returns_empty_set(self):
        """Return an empty set when the TRV state is missing."""
        assert get_current_set_temperatures(_fake_self(), None, "test") == set()


class TestCelsiusToSystemTemperature:
    """Outbound writes are expressed in the configured system unit."""

    @staticmethod
    def _hass(temperature_unit):
        """Create a mock hass with the given system temperature unit."""
        hass = Mock()
        hass.config.units.temperature_unit = temperature_unit
        return hass

    def test_celsius_system_returns_value_unchanged(self):
        """Pass the Celsius value through unchanged on a Celsius system."""
        hass = self._hass(UnitOfTemperature.CELSIUS)
        assert celsius_to_system_temperature(hass, 21.37) == 21.37

    def test_fahrenheit_system_converts_value(self):
        """Convert the Celsius value to Fahrenheit on a Fahrenheit system."""
        hass = self._hass(UnitOfTemperature.FAHRENHEIT)
        assert celsius_to_system_temperature(hass, 21.0) == 69.8

    def test_fahrenheit_conversion_rounds_to_one_decimal(self):
        """Round the Fahrenheit conversion result to one decimal place."""
        hass = self._hass(UnitOfTemperature.FAHRENHEIT)
        # 21.11 C is 69.998 F; the result is rounded to one decimal.
        assert celsius_to_system_temperature(hass, 21.11) == 70.0


class TestMatchesAnySetpoint:
    """Tolerance-based setpoint matching across float rounding grids."""

    def test_exact_match(self):
        """Match a value that equals a setpoint exactly."""
        assert matches_any_setpoint(20.7, {20.7}) is True

    def test_match_within_tolerance(self):
        """Match a value that deviates by less than the default tolerance."""
        # 0.005 is the worst legitimate write-vs-readback divergence
        # (half the 0.01 read grid).
        assert matches_any_setpoint(20.705, {20.7}) is True

    def test_no_match_outside_tolerance(self):
        """Reject a value that deviates by more than the default tolerance."""
        assert matches_any_setpoint(20.72, {20.7}) is False

    def test_no_match_on_adjacent_step(self):
        """Reject the adjacent setpoint one 0.1 step away."""
        # 0.1 is the smallest distinguishable setpoint step; the tolerance
        # must never conflate two distinct setpoints.
        assert matches_any_setpoint(20.8, {20.7}) is False

    def test_none_value_returns_false(self):
        """Reject a missing value."""
        assert matches_any_setpoint(None, {20.7}) is False

    def test_empty_set_returns_false(self):
        """Reject any value against an empty setpoint set."""
        assert matches_any_setpoint(20.7, set()) is False

    def test_matches_any_element_of_set(self):
        """Match when any element of the setpoint set is within tolerance."""
        assert matches_any_setpoint(21.0, {17.0, 21.0}) is True

    def test_step_grid_value_matches_read_grid_value(self):
        """Match a step-grid write against its read-back-grid counterpart."""
        # round_by_step(20.7, 0.1) yields 20.700000000000003, which is not
        # set-equal to the 20.7 produced by the 0.01 read-back grid.
        written = round_by_step(20.7, 0.1)
        assert written not in {20.7}
        assert matches_any_setpoint(written, {20.7}) is True

    def test_custom_tolerance_is_honored(self):
        """Honor a caller-supplied tolerance wider than the default."""
        assert matches_any_setpoint(20.9, {20.7}, tolerance=0.25) is True

    def test_default_tolerance_constant(self):
        """Pin the default tolerance constant at 0.01."""
        assert SETPOINT_MATCH_TOLERANCE == 0.01
