"""Tests for heating_power_valve_position calculation.

This module tests the heating power valve position calculation which uses
a heuristic formula to map temperature difference and heating power to
an expected valve opening percentage.
"""

import pytest

from custom_components.better_thermostat.utils.const import MIN_HEATING_POWER
from custom_components.better_thermostat.utils.helpers import (
    heating_power_valve_position,
)


class MockThermostat:
    """Mock Better Thermostat instance for testing."""

    def __init__(
        self,
        heat_target_temperature=20.0,
        room_temperature=18.0,
        heating_power=0.02,
        device_name="Test",
    ):
        """Initialize mock thermostat."""
        self.heat_target_temperature = heat_target_temperature
        self.room_temperature = room_temperature
        self.heating_power = heating_power
        self.device_name = device_name


class TestHeatingPowerValvePosition:
    """Test heating_power_valve_position function."""

    def test_returns_zero_when_target_equals_current(self):
        """Test that valve position is 0 when target temperature equals current temperature."""
        mock_bt = MockThermostat(heat_target_temperature=20.0, room_temperature=20.0)
        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        # When delta_kelvin is 0, formula gives 0
        assert result == 0.0

    @pytest.mark.parametrize(
        ("target", "current"), [(22.0, 20.0), (25.0, 15.0)], ids=["2K", "10K"]
    )
    def test_returns_value_between_0_and_1(self, target, current):
        """A demand beyond the formula's range opens the valve fully, not further.

        With 2 K or more to go at the default heating power the formula
        exceeds 1, and the result is capped at a fully open valve.
        """
        mock_bt = MockThermostat(
            heat_target_temperature=target, room_temperature=current, heating_power=0.02
        )
        assert (
            heating_power_valve_position(
                mock_bt, "climate.test", mock_bt.room_temperature
            )
            == 1.0
        )

    def test_higher_temp_diff_gives_higher_valve_position(self):
        """Test that larger temperature difference gives higher valve position."""
        mock_bt_small = MockThermostat(
            heat_target_temperature=20.5, room_temperature=20.0, heating_power=0.02
        )
        mock_bt_large = MockThermostat(
            heat_target_temperature=22.0, room_temperature=20.0, heating_power=0.02
        )

        result_small = heating_power_valve_position(
            mock_bt_small, "climate.test", mock_bt_small.room_temperature
        )
        result_large = heating_power_valve_position(
            mock_bt_large, "climate.test", mock_bt_large.room_temperature
        )

        assert result_large > result_small

    def test_lower_heating_power_gives_higher_valve_position(self):
        """Test that lower heating power (worse insulation) needs higher valve position."""
        # Better insulation (higher heating power value = less power needed)
        mock_bt_good_insulation = MockThermostat(
            heat_target_temperature=22.0, room_temperature=20.0, heating_power=0.03
        )
        # Worse insulation (lower heating power value = more power needed)
        mock_bt_poor_insulation = MockThermostat(
            heat_target_temperature=22.0, room_temperature=20.0, heating_power=0.01
        )

        result_good = heating_power_valve_position(
            mock_bt_good_insulation,
            "climate.test",
            mock_bt_good_insulation.room_temperature,
        )
        result_poor = heating_power_valve_position(
            mock_bt_poor_insulation,
            "climate.test",
            mock_bt_poor_insulation.room_temperature,
        )

        # Poor insulation needs higher valve position
        # Note: Both should be clamped to same minimum valve opening
        assert result_poor >= result_good

    def test_clamps_heating_power_to_min_max(self):
        """Test that heating_power is clamped to MIN/MAX values."""
        # Very low heating power (should be clamped to MIN)
        mock_bt_too_low = MockThermostat(
            heat_target_temperature=22.0, room_temperature=20.0, heating_power=0.0001
        )
        result_low = heating_power_valve_position(
            mock_bt_too_low, "climate.test", mock_bt_too_low.room_temperature
        )

        # Should be clamped to MIN_HEATING_POWER (0.001)
        # With MIN_HEATING_POWER, delta_kelvin=2.0 should give high valve position
        assert result_low > 0.5  # Should be fairly high

        # Very high heating power (should be clamped to MAX)
        mock_bt_too_high = MockThermostat(
            heat_target_temperature=22.0, room_temperature=20.0, heating_power=0.5
        )
        result_high = heating_power_valve_position(
            mock_bt_too_high, "climate.test", mock_bt_too_high.room_temperature
        )

        # Should be clamped to MAX_HEATING_POWER (0.1)
        assert result_high < 0.5  # Should be lower than unclamped

    def test_applies_minimum_valve_opening_for_large_diff(self):
        """Test that minimum valve opening is applied for delta_kelvin > 1.0°C."""
        # VALVE_MIN_THRESHOLD_TEMP_DIFF = 1.0
        # VALVE_MIN_OPENING_LARGE_DIFF = 0.15
        mock_bt = MockThermostat(
            heat_target_temperature=21.5, room_temperature=20.0, heating_power=0.05
        )
        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        # Should be at least VALVE_MIN_OPENING_LARGE_DIFF (15%)
        assert result >= 0.15

    def test_applies_proportional_minimum_for_small_diff(self):
        """Test proportional minimum for small temperature differences (0.2-1.0°C)."""
        # VALVE_MIN_SMALL_DIFF_THRESHOLD = 0.2
        mock_bt = MockThermostat(
            heat_target_temperature=20.5, room_temperature=20.0, heating_power=0.05
        )
        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        # Should have some minimum valve opening for 0.5°C diff
        assert result > 0.0

    def test_returns_zero_when_cooling_needed(self):
        """Test that valve returns 0% when room_temperature > heat_target_temperature."""
        mock_bt = MockThermostat(heat_target_temperature=20.0, room_temperature=22.0)

        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )
        assert result == 0.0

    def test_returns_zero_for_negative_temp_diff(self):
        """Test that negative temperature differences return 0% valve."""
        mock_bt = MockThermostat(
            heat_target_temperature=18.0, room_temperature=20.0, heating_power=0.02
        )

        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )
        assert result == 0.0

    def test_handles_very_small_temp_diff(self):
        """Test handling of very small temperature differences."""
        mock_bt = MockThermostat(
            heat_target_temperature=20.05, room_temperature=20.0, heating_power=0.02
        )
        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        # Should be valid but small
        assert 0.0 <= result <= 0.3

    def test_formula_produces_expected_values(self):
        """Test that the formula produces expected valve positions for known inputs."""
        # From the comments in the code:
        # With heating_power of 0.02 and delta_kelvin of 0.5, expect ~0.3992
        mock_bt = MockThermostat(
            heat_target_temperature=20.5, room_temperature=20.0, heating_power=0.02
        )
        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        # Allow some tolerance for float arithmetic and minimum valve logic
        assert 0.15 <= result <= 0.50

    def test_max_valve_position_for_large_difference(self):
        """Test that valve position reaches 100% for very large temperature differences."""
        mock_bt = MockThermostat(
            heat_target_temperature=25.0, room_temperature=15.0, heating_power=0.001
        )
        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        # With 10°C difference and low heating power, should be at or near 100%
        assert result >= 0.95

    @pytest.mark.parametrize("heating_power", [0.0, -0.01], ids=["zero", "negative"])
    def test_zero_heating_power(self, heating_power):
        """A heating power at or below zero is read as the lowest plausible one.

        The formula divides by the heating power, so an unlearned or corrupt
        value is raised to MIN_HEATING_POWER before it is used.
        """
        at_floor = heating_power_valve_position(
            MockThermostat(
                heat_target_temperature=20.1,
                room_temperature=20.0,
                heating_power=MIN_HEATING_POWER,
            ),
            "climate.test",
            20.0,
        )
        mock_bt = MockThermostat(
            heat_target_temperature=20.1,
            room_temperature=20.0,
            heating_power=heating_power,
        )

        result = heating_power_valve_position(
            mock_bt, "climate.test", mock_bt.room_temperature
        )

        assert 0.0 < result < 1.0
        assert result == pytest.approx(at_floor)

    @pytest.mark.parametrize(
        ("target", "room"), [(21.0, None), (None, 20.0)], ids=["no_room", "no_target"]
    )
    def test_missing_reading_returns_none(self, target, room):
        """Without a room temperature or a heating target the valve is not sized."""
        mock_bt = MockThermostat(heat_target_temperature=target, room_temperature=room)
        assert heating_power_valve_position(mock_bt, "climate.test", room) is None

    def test_uses_the_passed_room_temperature(self):
        """The passed reading drives the demand, not the entity's own attribute."""
        mock_bt = MockThermostat(heat_target_temperature=21.0, room_temperature=None)
        assert heating_power_valve_position(
            mock_bt, "climate.test", 20.5
        ) == pytest.approx(0.3992, abs=1e-4)
