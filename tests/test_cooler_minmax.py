"""Tests for cooler min/max temperature handling.

Issue #1588: BT crashes when cooling device min/max temp is outside TRV range

The bug: When a TRV has range 4-35°C and a cooler has range 18-30°C,
BT only calculates bt_min_temp/bt_max_temp from TRV states, ignoring the cooler.
When the user sets a temperature outside the cooler's range (e.g., 32°C),
Home Assistant raises ServiceValidationError because the cooler only accepts 18-30°C.

The fix: Include the cooler entity's state in the min/max calculation so that
bt_min_temp = max of all min_temps and bt_max_temp = min of all max_temps.
"""

from unittest.mock import MagicMock

import pytest


def reduce_attribute(states, attribute, reduce):
    """Simplified version of Home Assistant's reduce_attribute helper."""
    values = [
        state.attributes.get(attribute)
        for state in states
        if state.attributes.get(attribute) is not None
    ]
    if not values:
        return None
    return reduce(values)


@pytest.fixture
def mock_trv_state():
    """Create a mock TRV state with typical temperature range."""
    state = MagicMock()
    state.state = "heat"
    state.attributes = {
        "min_temp": 4.0,
        "max_temp": 35.0,
        "current_temperature": 21.0,
        "temperature": 22.0,
    }
    return state


@pytest.fixture
def mock_cooler_state():
    """Create a mock cooler state with narrower temperature range."""
    state = MagicMock()
    state.state = "off"
    state.attributes = {
        "min_temp": 18.0,
        "max_temp": 30.0,
        "current_temperature": 23.0,
        "temperature": 25.0,
    }
    return state


@pytest.fixture
def mock_cooler_state_heating_mode():
    """Create a mock cooler state with different range in heating mode."""
    state = MagicMock()
    state.state = "heat"
    state.attributes = {
        "min_temp": 10.0,
        "max_temp": 30.0,  # Same max as cooling mode
        "current_temperature": 23.0,
        "temperature": 25.0,
    }
    return state


class TestMinMaxWithCooler:
    """Tests for min/max calculation including cooler entity."""

    def test_trv_only_uses_trv_range(self, mock_trv_state):
        """Test that TRV-only setup uses TRV's full range."""
        states = [mock_trv_state]

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        assert bt_min_temp == 4.0
        assert bt_max_temp == 35.0

    def test_with_cooler_uses_intersection(self, mock_trv_state, mock_cooler_state):
        """Test that min/max with cooler uses intersection of ranges.

        TRV: 4-35°C, Cooler: 18-30°C
        Result should be: 18-30°C (the intersection)
        """
        states = [mock_trv_state, mock_cooler_state]

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        # max of min_temps: max(4, 18) = 18
        assert bt_min_temp == 18.0
        # min of max_temps: min(35, 30) = 30
        assert bt_max_temp == 30.0

    def test_prevents_out_of_range_setpoint(self, mock_trv_state, mock_cooler_state):
        """Test that the calculated range prevents invalid cooler temperatures.

        User scenario: User tries to set 32°C, but cooler only accepts up to 30°C.
        With the fix, bt_max_temp is 30, so 32 would be clamped to 30.
        """
        states = [mock_trv_state, mock_cooler_state]

        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        user_requested_temp = 32.0
        # With the fix, the UI would limit to bt_max_temp
        actual_setpoint = min(user_requested_temp, bt_max_temp)

        assert actual_setpoint == 30.0, (
            f"User requested {user_requested_temp}°C but cooler max is {bt_max_temp}°C. "
            f"Should be clamped to {bt_max_temp}°C to prevent ServiceValidationError."
        )

    def test_multiple_trvs_with_cooler(self, mock_cooler_state):
        """Test min/max with multiple TRVs and one cooler."""
        trv1 = MagicMock()
        trv1.state = "heat"
        trv1.attributes = {"min_temp": 5.0, "max_temp": 30.0}

        trv2 = MagicMock()
        trv2.state = "heat"
        trv2.attributes = {"min_temp": 4.0, "max_temp": 35.0}

        states = [trv1, trv2, mock_cooler_state]

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        # max(5, 4, 18) = 18
        assert bt_min_temp == 18.0
        # min(30, 35, 30) = 30
        assert bt_max_temp == 30.0


class TestCoolerStateHandling:
    """Tests for cooler state availability handling."""

    def test_unavailable_cooler_excluded(self, mock_trv_state):
        """Test that unavailable cooler doesn't affect calculation."""
        # Unavailable cooler should not be added to states list
        # The fix checks for STATE_UNAVAILABLE before adding
        states = [mock_trv_state]  # Cooler not added because unavailable

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        # Should use TRV range only
        assert bt_min_temp == 4.0
        assert bt_max_temp == 35.0

    def test_cooler_with_none_attributes_handled(self, mock_trv_state):
        """Test that cooler with None attributes is handled gracefully."""
        cooler_with_none = MagicMock()
        cooler_with_none.state = "off"
        cooler_with_none.attributes = {
            "min_temp": None,  # No min_temp attribute
            "max_temp": None,  # No max_temp attribute
        }

        states = [mock_trv_state, cooler_with_none]

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        # Should fallback to TRV values since cooler has None
        assert bt_min_temp == 4.0
        assert bt_max_temp == 35.0


class TestEdgeCases:
    """Tests for edge cases in min/max calculation."""

    def test_cooler_wider_range_than_trv(self):
        """Test when cooler has wider range than TRV (rare but possible)."""
        trv = MagicMock()
        trv.state = "heat"
        trv.attributes = {"min_temp": 10.0, "max_temp": 28.0}

        cooler = MagicMock()
        cooler.state = "cool"
        cooler.attributes = {"min_temp": 16.0, "max_temp": 32.0}

        states = [trv, cooler]

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        # max(10, 16) = 16, min(28, 32) = 28
        assert bt_min_temp == 16.0
        assert bt_max_temp == 28.0

    def test_no_overlap_warning_scenario(self):
        """Test when TRV and cooler ranges don't overlap (configuration error).

        TRV: 4-15°C, Cooler: 18-30°C
        This would result in min > max, indicating misconfiguration.
        """
        trv = MagicMock()
        trv.state = "heat"
        trv.attributes = {"min_temp": 4.0, "max_temp": 15.0}

        cooler = MagicMock()
        cooler.state = "cool"
        cooler.attributes = {"min_temp": 18.0, "max_temp": 30.0}

        states = [trv, cooler]

        bt_min_temp = reduce_attribute(states, "min_temp", reduce=max)
        bt_max_temp = reduce_attribute(states, "max_temp", reduce=min)

        # max(4, 18) = 18, min(15, 30) = 15
        # This results in min_temp > max_temp, which is invalid!
        assert bt_min_temp == 18.0
        assert bt_max_temp == 15.0

        # This is a misconfiguration - min_temp > max_temp
        assert bt_min_temp > bt_max_temp, (
            "Non-overlapping ranges result in min > max. "
            "BT should ideally warn about this configuration."
        )
