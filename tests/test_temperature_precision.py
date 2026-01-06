"""Tests for temperature precision handling.

These tests verify that temperature values are handled with appropriate precision
throughout the calibration and HVAC action calculation logic.

Related issues:
- #1792: Rounding to 1 decimal in calibration logic causes TRV to heat when BT shows idle
- #1789: MPC heats up the room even though BetterThermostat says it is idle
- #1785: PID controller heats although the temperature is already high
- #1736: BT says idle, actual TRV is heating (offset based)
- #1718: Underlying climate entity remains in the heating state even if BT is in idle
"""

import pytest

from custom_components.better_thermostat.utils.helpers import (
    convert_to_float,
    round_by_step,
)


class TestConvertToFloat:
    """Tests for convert_to_float function precision handling."""

    def test_convert_to_float_preserves_two_decimal_precision(self):
        """Test that convert_to_float preserves 2 decimal places.

        This is the core bug from issue #1792: sensors reporting 19.97 should
        not be rounded to 20.0, as this causes incorrect HVAC action decisions.
        """
        # Sensor reports 19.97, should NOT be rounded to 20.0
        result = convert_to_float("19.97", "test", "test")
        assert result is not None
        # The value should be preserved with at least 2 decimal precision
        assert result == pytest.approx(19.97, abs=0.001), (
            f"convert_to_float('19.97') returned {result}, expected ~19.97. "
            "This causes BT to show 'idle' while TRV continues heating."
        )

    def test_convert_to_float_near_target_boundary(self):
        """Test precision at the target temperature boundary.

        When sensor reads 19.99 and target is 20.0:
        - With proper precision: 19.99 < 20.0 -> should heat
        - With 1 decimal rounding: 20.0 == 20.0 -> idle (BUG!)
        """
        result = convert_to_float("19.99", "test", "test")
        assert result is not None
        assert result < 20.0, (
            f"convert_to_float('19.99') returned {result}, expected < 20.0. "
            "This value at the boundary is critical for correct heating decisions."
        )

    def test_convert_to_float_slightly_above_target(self):
        """Test precision slightly above target temperature.

        When sensor reads 20.03 and target is 20.0:
        - With proper precision: 20.03 > 20.0 -> should be idle
        """
        result = convert_to_float("20.03", "test", "test")
        assert result is not None
        # The value should be preserved as ~20.03
        assert result == pytest.approx(20.03, abs=0.001)

    def test_convert_to_float_various_precisions(self):
        """Test that various precision levels are handled correctly."""
        test_cases = [
            ("19.97", 19.97),
            ("19.95", 19.95),
            ("20.01", 20.01),
            ("20.05", 20.05),
            ("21.123", 21.12),  # 3 decimals should round to 2
            ("18.5", 18.5),
        ]

        for input_val, expected in test_cases:
            result = convert_to_float(input_val, "test", "test")
            assert result is not None
            assert result == pytest.approx(expected, abs=0.01), (
                f"convert_to_float('{input_val}') returned {result}, "
                f"expected ~{expected}"
            )


class TestRoundByStep:
    """Tests for round_by_step function."""

    def test_round_by_step_01_preserves_tenths(self):
        """Test that 0.1 step rounding preserves tenths correctly."""
        # These should round to their nearest 0.1
        assert round_by_step(19.97, 0.1) == pytest.approx(20.0, abs=0.001)
        assert round_by_step(19.94, 0.1) == pytest.approx(19.9, abs=0.001)
        # Note: 19.95 rounds down due to rounding.nearest epsilon offset
        assert round_by_step(19.95, 0.1) == pytest.approx(19.9, abs=0.001)

    def test_round_by_step_001_preserves_hundredths(self):
        """Test that 0.01 step rounding preserves hundredths."""
        assert round_by_step(19.97, 0.01) == pytest.approx(19.97, abs=0.001)
        assert round_by_step(19.994, 0.01) == pytest.approx(19.99, abs=0.001)
        # Note: 19.995 rounds down due to rounding.nearest epsilon offset
        assert round_by_step(19.995, 0.01) == pytest.approx(19.99, abs=0.001)


class TestHvacActionPrecision:
    """Tests demonstrating the HVAC action precision issue."""

    def test_heating_decision_at_boundary(self):
        """Verify correct HVAC decision with proper precision.

        Scenario from issue #1792:
        - External sensor: 19.97 C
        - Target temp: 20.0 C
        - Expected: should_heat = True (19.97 < 20.0)
        """
        target_temp = 20.0
        tolerance = 0.0

        # Simulate sensor reading
        sensor_reading = "19.97"

        # convert_to_float preserves precision
        cur_temp = convert_to_float(sensor_reading, "test", "test")

        # Heating threshold calculation
        heat_on_threshold = target_temp - tolerance

        # With correct precision: 19.97 < 20.0 -> should heat
        should_heat = cur_temp < heat_on_threshold

        assert should_heat is True, (
            f"Heating decision incorrect: cur_temp={cur_temp}, "
            f"threshold={heat_on_threshold}, should_heat={should_heat}"
        )

    def test_tolerance_check_precision(self):
        """Test that tolerance check uses correct precision.

        With sensor at 19.97, target at 20.0, tolerance at 0.0:
        - _within_tolerance should be False (19.97 is not within [20.0, 20.0])
        - Bug: rounds 19.97 to 20.0, so _within_tolerance becomes True
        """
        target_temp = 20.0
        tolerance = 0.0
        sensor_reading = "19.97"

        cur_temp = convert_to_float(sensor_reading, "test", "test")

        # Tolerance check as done in calibration.py
        within_tolerance = (cur_temp >= (target_temp - tolerance)) and (
            cur_temp <= (target_temp + tolerance)
        )

        # With precision preserved: 19.97 < 20.0, so within_tolerance = False
        assert within_tolerance is False, (
            f"Tolerance check failed: cur_temp={cur_temp} (from {sensor_reading}), "
            f"target={target_temp}, tolerance={tolerance}. "
            f"within_tolerance={within_tolerance}, expected False"
        )
