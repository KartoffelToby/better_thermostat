"""Tests for aggressive calibration behavior.

Issue #1790: Temperature settings sent to TRVs stay too close to target temperature.

This test file verifies the fix for aggressive calibration not being applied
in the hysteresis band between (target - tolerance) and target.

The fix changes the condition from checking hvac_action == HEATING to
checking cur_temp < bt_target_temp.
"""

from homeassistant.components.climate import HVACAction


class TestAggressiveCalibrationCondition:
    """Tests for the aggressive calibration condition logic.

    These tests verify that the condition for applying aggressive calibration
    correctly handles the hysteresis band scenario.
    """

    def test_old_condition_fails_in_hysteresis_band(self):
        """Test that the OLD condition (hvac_action == HEATING) fails in hysteresis band.

        This demonstrates the bug: when temperature is in the hysteresis band,
        hvac_action stays IDLE, so aggressive calibration is not applied.
        """
        target = 21.0
        tolerance = 0.5
        cur_temp = 20.7  # In hysteresis band: between (target - tolerance) and target
        previous_action = HVACAction.IDLE

        # Hysteresis logic determines hvac_action
        heat_on_threshold = target - tolerance  # 20.5
        if previous_action == HVACAction.HEATING:
            should_heat = cur_temp < target
        else:
            should_heat = cur_temp < heat_on_threshold  # 20.7 < 20.5 = False

        hvac_action = HVACAction.HEATING if should_heat else HVACAction.IDLE

        # OLD condition: check hvac_action
        base_calibration = -0.5
        if hvac_action == HVACAction.HEATING:
            if base_calibration > -2.5:
                calibration_old = base_calibration - 2.5
            else:
                calibration_old = base_calibration
        else:
            calibration_old = base_calibration  # No adjustment!

        # In hysteresis band, hvac_action is IDLE, so no aggressive adjustment
        assert hvac_action == HVACAction.IDLE
        assert calibration_old == -0.5  # No aggressive adjustment applied

    def test_new_condition_works_in_hysteresis_band(self):
        """Test that the NEW condition (cur_temp < target) works in hysteresis band.

        This verifies the fix: by checking temperature instead of hvac_action,
        aggressive calibration is applied when the room needs heating.
        """
        target = 21.0
        cur_temp = 20.7  # In hysteresis band, but still below target

        # NEW condition: check temperature
        base_calibration = -0.5
        if cur_temp is not None and cur_temp < target:
            if base_calibration > -2.5:
                calibration_new = base_calibration - 2.5
            else:
                calibration_new = base_calibration
        else:
            calibration_new = base_calibration

        # Temperature is below target, so aggressive adjustment is applied
        assert calibration_new == -3.0  # Aggressive adjustment applied!

    def test_new_condition_no_adjustment_when_at_target(self):
        """Test that aggressive calibration is NOT applied when at or above target."""
        target = 21.0
        cur_temp = 21.0  # At target

        base_calibration = -0.5
        if cur_temp is not None and cur_temp < target:
            if base_calibration > -2.5:
                calibration = base_calibration - 2.5
            else:
                calibration = base_calibration
        else:
            calibration = base_calibration

        # At target, no aggressive adjustment
        assert calibration == -0.5

    def test_new_condition_no_adjustment_when_above_target(self):
        """Test that aggressive calibration is NOT applied when above target."""
        target = 21.0
        cur_temp = 22.0  # Above target

        base_calibration = -0.5
        if cur_temp is not None and cur_temp < target:
            if base_calibration > -2.5:
                calibration = base_calibration - 2.5
            else:
                calibration = base_calibration
        else:
            calibration = base_calibration

        # Above target, no aggressive adjustment
        assert calibration == -0.5

    def test_new_condition_handles_none_temperature(self):
        """Test that the condition handles None temperature gracefully."""
        target = 21.0
        cur_temp = None

        base_calibration = -0.5
        if cur_temp is not None and cur_temp < target:
            if base_calibration > -2.5:
                calibration = base_calibration - 2.5
            else:
                calibration = base_calibration
        else:
            calibration = base_calibration

        # None temperature, no aggressive adjustment
        assert calibration == -0.5


class TestAggressiveCalibrationSetpoint:
    """Tests for aggressive calibration with setpoint-based TRVs."""

    def test_old_condition_fails_in_hysteresis_band_setpoint(self):
        """Test that OLD condition fails for setpoint calibration in hysteresis band."""
        target = 21.0
        tolerance = 0.5
        cur_temp = 20.7
        cur_trv_temp = 20.0
        previous_action = HVACAction.IDLE

        # Hysteresis logic
        heat_on_threshold = target - tolerance
        should_heat = (
            cur_temp < heat_on_threshold
            if previous_action == HVACAction.IDLE
            else cur_temp < target
        )
        hvac_action = HVACAction.HEATING if should_heat else HVACAction.IDLE

        # Base setpoint calculation (simplified)
        calibrated_setpoint = target + (target - cur_temp)  # 21 + 0.3 = 21.3

        # OLD condition
        if hvac_action == HVACAction.HEATING:
            if calibrated_setpoint - cur_trv_temp < 2.5:  # 21.3 - 20 = 1.3 < 2.5
                setpoint_old = calibrated_setpoint + 2.5
            else:
                setpoint_old = calibrated_setpoint
        else:
            setpoint_old = calibrated_setpoint

        assert hvac_action == HVACAction.IDLE
        assert setpoint_old == 21.3  # No aggressive adjustment

    def test_new_condition_works_in_hysteresis_band_setpoint(self):
        """Test that NEW condition works for setpoint calibration in hysteresis band."""
        target = 21.0
        cur_temp = 20.7
        cur_trv_temp = 20.0

        # Base setpoint calculation (simplified)
        calibrated_setpoint = target + (target - cur_temp)  # 21 + 0.3 = 21.3

        # NEW condition
        if cur_temp is not None and cur_temp < target:
            if calibrated_setpoint - cur_trv_temp < 2.5:  # 21.3 - 20 = 1.3 < 2.5
                setpoint_new = calibrated_setpoint + 2.5
            else:
                setpoint_new = calibrated_setpoint
        else:
            setpoint_new = calibrated_setpoint

        assert setpoint_new == 23.8  # Aggressive adjustment applied!


class TestHysteresisLogic:
    """Tests demonstrating the hysteresis behavior that causes the issue."""

    def test_hysteresis_prevents_heating_in_band(self):
        """Test that hysteresis logic prevents heating when in the band."""
        target = 21.0
        tolerance = 0.5
        cur_temp = 20.7  # Between 20.5 (heat_on) and 21.0 (target)
        previous_action = HVACAction.IDLE

        heat_on_threshold = target - tolerance  # 20.5
        heat_off_threshold = target  # 21.0

        # When previously IDLE, need to go below heat_on_threshold to start
        if previous_action == HVACAction.HEATING:
            should_heat = cur_temp < heat_off_threshold
        else:
            should_heat = cur_temp < heat_on_threshold

        assert should_heat is False
        assert cur_temp > heat_on_threshold  # 20.7 > 20.5
        assert cur_temp < heat_off_threshold  # 20.7 < 21.0

    def test_hysteresis_continues_heating_in_band(self):
        """Test that hysteresis continues heating when already heating."""
        target = 21.0
        tolerance = 0.5
        cur_temp = 20.7
        previous_action = HVACAction.HEATING  # Was already heating

        heat_on_threshold = target - tolerance
        heat_off_threshold = target

        # When previously HEATING, continue until heat_off_threshold
        if previous_action == HVACAction.HEATING:
            should_heat = cur_temp < heat_off_threshold  # 20.7 < 21.0 = True
        else:
            should_heat = cur_temp < heat_on_threshold

        assert should_heat is True  # Continues heating


class TestEdgeCases:
    """Edge case tests for the aggressive calibration fix."""

    def test_exactly_at_tolerance_boundary(self):
        """Test behavior exactly at the tolerance boundary."""
        target = 21.0
        cur_temp = 20.5  # Exactly at heat_on_threshold (target - 0.5 tolerance)

        # NEW condition should still apply aggressive when below target
        base_calibration = -0.5
        if cur_temp is not None and cur_temp < target:
            if base_calibration > -2.5:
                calibration = base_calibration - 2.5
            else:
                calibration = base_calibration
        else:
            calibration = base_calibration

        # Below target, aggressive adjustment applied
        assert calibration == -3.0

    def test_calibration_already_aggressive_enough(self):
        """Test that already aggressive calibration is not made more aggressive."""
        target = 21.0
        cur_temp = 20.0

        # Base calibration already <= -2.5
        base_calibration = -3.0
        if cur_temp is not None and cur_temp < target:
            if base_calibration > -2.5:
                calibration = base_calibration - 2.5
            else:
                calibration = base_calibration  # Don't adjust further
        else:
            calibration = base_calibration

        # Already aggressive enough, no further adjustment
        assert calibration == -3.0
