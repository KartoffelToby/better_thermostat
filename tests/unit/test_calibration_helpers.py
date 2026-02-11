"""Tests for calibration.py helper functions."""

from unittest.mock import MagicMock, patch

import pytest

from custom_components.better_thermostat.calibration import (
    _get_current_outdoor_temp,
    _get_current_solar_intensity,
    _get_trv_max_opening,
    _supports_direct_valve_control,
)
from custom_components.better_thermostat.utils.const import CalibrationType


class TestGetCurrentOutdoorTemp:
    """Test _get_current_outdoor_temp function."""

    def test_returns_temp_from_outdoor_sensor(self):
        """Test that function returns temperature from outdoor sensor."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.outdoor_sensor = "sensor.outdoor_temp"
        mock_self.weather_entity = None

        mock_state = MagicMock()
        mock_state.state = "15.5"
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_outdoor_temp(mock_self)

        assert result == 15.5

    def test_returns_temp_from_weather_entity_when_no_sensor(self):
        """Test that function returns temperature from weather entity."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = "weather.home"

        mock_state = MagicMock()
        mock_state.attributes = {"temperature": 12.3}
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_outdoor_temp(mock_self)

        assert result == 12.3

    def test_returns_none_when_no_source_available(self):
        """Test that function returns None when no outdoor source is available."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.outdoor_sensor = None
        mock_self.weather_entity = None

        result = _get_current_outdoor_temp(mock_self)

        assert result is None

    def test_prefers_outdoor_sensor_over_weather(self):
        """Test that outdoor sensor is preferred over weather entity."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.outdoor_sensor = "sensor.outdoor_temp"
        mock_self.weather_entity = "weather.home"

        def mock_states_get(entity_id):
            if entity_id == "sensor.outdoor_temp":
                state = MagicMock()
                state.state = "10.0"
                return state
            if entity_id == "weather.home":
                state = MagicMock()
                state.attributes = {"temperature": 20.0}
                return state
            return None

        mock_self.hass.states.get.side_effect = mock_states_get

        result = _get_current_outdoor_temp(mock_self)

        assert result == 10.0


class TestGetCurrentSolarIntensity:
    """Test _get_current_solar_intensity function."""

    def test_returns_zero_when_no_weather_entity(self):
        """Test that function returns 0.0 when no weather entity."""
        mock_self = MagicMock()
        mock_self.weather_entity = None

        result = _get_current_solar_intensity(mock_self)

        assert result == 0.0

    def test_returns_intensity_based_on_cloud_coverage(self):
        """Test that function calculates intensity from cloud coverage."""
        mock_self = MagicMock()
        mock_self.weather_entity = "weather.home"

        mock_state = MagicMock()
        mock_state.attributes = {"cloud_coverage": 25}  # 25% clouds = 0.75 intensity
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_solar_intensity(mock_self)

        assert result == 0.75

    def test_returns_intensity_based_on_uv_index(self):
        """Test that function calculates intensity from UV index."""
        mock_self = MagicMock()
        mock_self.weather_entity = "weather.home"

        mock_state = MagicMock()
        mock_state.attributes = {"uv_index": 5.0}  # UV 5 = 0.5 intensity
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_solar_intensity(mock_self)

        assert result == 0.5

    def test_returns_high_intensity_for_sunny_condition(self):
        """Test that function returns 1.0 for sunny condition."""
        mock_self = MagicMock()
        mock_self.weather_entity = "weather.home"

        mock_state = MagicMock()
        mock_state.state = "sunny"
        mock_state.attributes = {}
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_solar_intensity(mock_self)

        assert result == 1.0

    def test_returns_medium_intensity_for_partlycloudy(self):
        """Test that function returns 0.7 for partly cloudy condition."""
        mock_self = MagicMock()
        mock_self.weather_entity = "weather.home"

        mock_state = MagicMock()
        mock_state.state = "partlycloudy"
        mock_state.attributes = {}
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_solar_intensity(mock_self)

        assert result == 0.7

    def test_returns_low_intensity_for_cloudy(self):
        """Test that function returns 0.4 for cloudy condition."""
        mock_self = MagicMock()
        mock_self.weather_entity = "weather.home"

        mock_state = MagicMock()
        mock_state.state = "cloudy"
        mock_state.attributes = {}
        mock_self.hass.states.get.return_value = mock_state

        result = _get_current_solar_intensity(mock_self)

        assert result == 0.4


class TestSupportsDirectValveControl:
    """Test _supports_direct_valve_control function."""

    def test_returns_false_when_not_direct_valve_based(self):
        """Test that function returns False when calibration type is not DIRECT_VALVE_BASED."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.real_trvs = {
            "climate.test_trv": {
                "advanced": {"calibration": CalibrationType.LOCAL_BASED}
            }
        }

        result = _supports_direct_valve_control(mock_self, "climate.test_trv")

        assert result is False

    def test_returns_true_when_valve_entity_writable(self):
        """Test that function returns True when valve entity is writable."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.real_trvs = {
            "climate.test_trv": {
                "advanced": {"calibration": CalibrationType.DIRECT_VALVE_BASED},
                "valve_position_entity": "number.test_valve",
                "valve_position_writable": True,
            }
        }

        result = _supports_direct_valve_control(mock_self, "climate.test_trv")

        assert result is True

    def test_returns_true_when_override_set_valve_exists(self):
        """Test that function returns True when override_set_valve exists."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"

        mock_quirks = MagicMock()
        mock_quirks.override_set_valve = MagicMock()

        mock_self.real_trvs = {
            "climate.test_trv": {
                "advanced": {"calibration": CalibrationType.DIRECT_VALVE_BASED},
                "valve_position_entity": None,
                "valve_position_writable": False,
                "model_quirks": mock_quirks,
            }
        }

        result = _supports_direct_valve_control(mock_self, "climate.test_trv")

        assert result is True

    def test_returns_false_when_valve_not_writable(self):
        """Test that function returns False when valve is not writable."""
        mock_self = MagicMock()
        mock_self.device_name = "Test Thermostat"
        mock_self.real_trvs = {
            "climate.test_trv": {
                "advanced": {"calibration": CalibrationType.DIRECT_VALVE_BASED},
                "valve_position_entity": "number.test_valve",
                "valve_position_writable": False,
            }
        }

        result = _supports_direct_valve_control(mock_self, "climate.test_trv")

        assert result is False


class TestGetTrvMaxOpening:
    """Test _get_trv_max_opening function."""

    def test_returns_configured_max_opening(self):
        """Test that function returns configured max opening."""
        mock_self = MagicMock()
        mock_self.real_trvs = {
            "climate.test_trv": {"valve_max_opening": 80.0}
        }

        result = _get_trv_max_opening(mock_self, "climate.test_trv")

        assert result == 80.0

    def test_clamps_max_opening_to_100(self):
        """Test that max opening is clamped to 100."""
        mock_self = MagicMock()
        mock_self.real_trvs = {
            "climate.test_trv": {"valve_max_opening": 150.0}
        }

        result = _get_trv_max_opening(mock_self, "climate.test_trv")

        assert result == 100.0

    def test_clamps_max_opening_to_zero(self):
        """Test that max opening is clamped to 0."""
        mock_self = MagicMock()
        mock_self.real_trvs = {
            "climate.test_trv": {"valve_max_opening": -10.0}
        }

        result = _get_trv_max_opening(mock_self, "climate.test_trv")

        assert result == 0.0

    def test_returns_none_when_not_configured(self):
        """Test that function returns None when max opening is not configured."""
        mock_self = MagicMock()
        mock_self.real_trvs = {
            "climate.test_trv": {}
        }

        result = _get_trv_max_opening(mock_self, "climate.test_trv")

        assert result is None

    def test_returns_none_when_invalid_type(self):
        """Test that function returns None when max opening is invalid type."""
        mock_self = MagicMock()
        mock_self.real_trvs = {
            "climate.test_trv": {"valve_max_opening": "invalid"}
        }

        result = _get_trv_max_opening(mock_self, "climate.test_trv")

        assert result is None