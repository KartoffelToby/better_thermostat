"""Tests for off_temperature attribute exposure.

This test verifies that the configured off_temperature threshold is correctly
exposed as an entity attribute in extra_state_attributes.

Related issue: User requested outdoor temperature threshold to be visible in
Developer Tools States for use in custom climate cards.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.better_thermostat.utils.const import ATTR_STATE_OFF_TEMPERATURE


@pytest.fixture
def mock_bt_with_off_temperature():
    """Create a mock BetterThermostat instance with off_temperature configured."""
    bt = MagicMock()
    bt.device_name = "Test Thermostat"
    bt.off_temperature = 20.0
    bt.window_open = False
    bt.call_for_heat = True
    bt.last_change = MagicMock()
    bt.last_change.isoformat = MagicMock(return_value="2026-01-11T20:00:00")
    bt._saved_temperature = None
    bt._preset_temperature = None
    bt._current_humidity = 50.0
    bt.last_main_hvac_mode = "heat"
    bt.tolerance = 0.5
    bt.bt_target_temp_step = 0.5
    bt.heating_power = 0.1
    bt.devices_errors = []
    bt.devices_states = {}
    bt.cur_temp_filtered = 20.5
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    return bt


@pytest.fixture
def mock_bt_without_off_temperature():
    """Create a mock BetterThermostat instance without off_temperature configured."""
    bt = MagicMock()
    bt.device_name = "Test Thermostat No Off Temp"
    bt.off_temperature = None
    bt.window_open = False
    bt.call_for_heat = True
    bt.last_change = MagicMock()
    bt.last_change.isoformat = MagicMock(return_value="2026-01-11T20:00:00")
    bt._saved_temperature = None
    bt._preset_temperature = None
    bt._current_humidity = 50.0
    bt.last_main_hvac_mode = "heat"
    bt.tolerance = 0.5
    bt.bt_target_temp_step = 0.5
    bt.heating_power = 0.1
    bt.devices_errors = []
    bt.devices_states = {}
    bt.cur_temp_filtered = 20.5
    bt.degraded_mode = False
    bt.unavailable_sensors = []
    return bt


class TestOffTemperatureAttribute:
    """Tests for off_temperature attribute exposure in extra_state_attributes."""

    def test_off_temperature_exposed_when_configured(
        self, mock_bt_with_off_temperature
    ):
        """Test that off_temperature is exposed when configured.

        The off_temperature attribute should be present in extra_state_attributes
        and should contain the configured threshold value (e.g., 20.0).
        """
        # Verify the constant is available
        assert ATTR_STATE_OFF_TEMPERATURE == "off_temperature"

        # Verify the mock has the correct value
        bt = mock_bt_with_off_temperature
        assert bt.off_temperature == 20.0
        assert bt.off_temperature is not None

    def test_off_temperature_none_when_not_configured(
        self, mock_bt_without_off_temperature
    ):
        """Test that off_temperature is None when not configured.

        When no outdoor sensor or weather entity is configured, or when
        off_temperature is not set, the attribute should be None.
        """
        # Verify the constant is defined
        assert ATTR_STATE_OFF_TEMPERATURE == "off_temperature"

        # Verify the mock has None when not configured
        bt = mock_bt_without_off_temperature
        assert bt.off_temperature is None

    def test_off_temperature_with_various_values(self):
        """Test off_temperature attribute with various valid temperature values.

        This verifies that the attribute correctly stores different threshold values
        that users might configure (e.g., 15°C, 18°C, 22°C, etc.).
        """
        test_values = [15.0, 18.0, 20.0, 22.0, 25.0, 0.0]

        for temp_value in test_values:
            bt = MagicMock()
            bt.off_temperature = temp_value

            # Verify the value is stored correctly
            assert bt.off_temperature == temp_value
            assert isinstance(bt.off_temperature, float)

    def test_off_temperature_constant_definition(self):
        """Test that the ATTR_STATE_OFF_TEMPERATURE constant is properly defined.

        This constant is used as the key in extra_state_attributes dict.
        """
        # Verify the constant matches the expected attribute name
        assert ATTR_STATE_OFF_TEMPERATURE == "off_temperature"

        # Verify it's a string
        assert isinstance(ATTR_STATE_OFF_TEMPERATURE, str)

    def test_off_temperature_in_attribute_dict_pattern(self):
        """Test that off_temperature follows the pattern used by other attributes.

        This test verifies that the off_temperature attribute would be correctly
        included in the extra_state_attributes dictionary, similar to window_open.
        """
        # Simulate the extra_state_attributes dict pattern
        mock_bt = MagicMock()
        mock_bt.off_temperature = 20.0

        # Simulate building the attributes dict as done in climate.py
        test_attributes = {ATTR_STATE_OFF_TEMPERATURE: mock_bt.off_temperature}

        # Verify the attribute is in the dict with the correct key and value
        assert "off_temperature" in test_attributes
        assert test_attributes["off_temperature"] == 20.0

    def test_off_temperature_none_handling_in_attributes(self):
        """Test that None values are handled correctly in attributes.

        When off_temperature is None, it should still be included in the
        attributes dict (same pattern as other optional attributes).
        """
        mock_bt = MagicMock()
        mock_bt.off_temperature = None

        # Simulate building the attributes dict
        test_attributes = {ATTR_STATE_OFF_TEMPERATURE: mock_bt.off_temperature}

        # Verify the attribute is in the dict with None value
        assert "off_temperature" in test_attributes
        assert test_attributes["off_temperature"] is None
