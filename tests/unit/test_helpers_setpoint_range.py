"""Tests for the range-setpoint helpers in utils/helpers.py."""

from unittest.mock import Mock

from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.const import UnitOfTemperature
from homeassistant.core import State

from custom_components.better_thermostat.utils.helpers import (
    get_current_set_temperatures,
    trv_supports_temperature_range,
)

RANGE_BIT = int(ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)


def _fake_self():
    """Create a minimal BetterThermostat mock for attr_to_celsius."""
    mock_self = Mock()
    mock_self.device_name = "test_thermostat"
    mock_self.hass.config.units.temperature_unit = UnitOfTemperature.CELSIUS
    return mock_self


class TestTrvSupportsTemperatureRange:
    """Feature detection reads the supported_features bitmask."""

    def test_none_state_returns_false(self):
        assert trv_supports_temperature_range(None) is False

    def test_missing_attribute_returns_false(self):
        state = State("climate.trv", "heat", {})
        assert trv_supports_temperature_range(state) is False

    def test_bit_not_set_returns_false(self):
        state = State("climate.trv", "heat", {"supported_features": 0})
        assert trv_supports_temperature_range(state) is False

    def test_bit_set_returns_true(self):
        state = State("climate.trv", "heat", {"supported_features": RANGE_BIT})
        assert trv_supports_temperature_range(state) is True


class TestGetCurrentSetTemperatures:
    """Setpoint collection honors the range feature bit."""

    def test_single_setpoint_only(self):
        state = State(
            "climate.trv", "heat", {"temperature": 20.0, "supported_features": 0}
        )
        assert get_current_set_temperatures(_fake_self(), state, "test") == {20.0}

    def test_range_low_included_when_supported(self):
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
        state = State(
            "climate.trv",
            "heat",
            {"temperature": 17.0, "target_temp_low": 21.0, "supported_features": 0},
        )
        assert get_current_set_temperatures(_fake_self(), state, "test") == {17.0}

    def test_none_state_returns_empty_set(self):
        assert get_current_set_temperatures(_fake_self(), None, "test") == set()
