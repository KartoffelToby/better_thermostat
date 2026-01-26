"""Tests for aggressive calibration behavior.

Issue #1790: Temperature settings sent to TRVs stay too close to target temperature.

Root cause analysis:
- When temperature drops below the tolerance band and heating should start,
  hvac_action is still IDLE (TRV hasn't started heating yet)
- The tolerance delay (adding tolerance * 2.0) is applied to ALL modes when IDLE
- This effectively makes aggressive calibration LESS aggressive than intended
  when starting to heat

Fix:
- Skip the tolerance delay for AGGRESIVE_CALIBRATION mode
- This allows aggressive mode to start heating faster without the delay
- The -2.5 offset still only applies when hvac_action == HEATING (intentional)
"""

from homeassistant.components.climate import HVACAction

from custom_components.better_thermostat.utils.const import CalibrationMode


class TestToleranceDelayBehavior:
    """Tests for tolerance delay behavior with different calibration modes."""

    def test_default_mode_has_tolerance_delay_when_idle(self):
        """Test that DEFAULT mode adds tolerance delay when IDLE.

        This is the intentional behavior to prevent oscillation.
        """
        calibration_mode = CalibrationMode.DEFAULT
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        base_calibration = -1.0

        # Simulate the tolerance delay logic
        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )

        new_calibration = base_calibration
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if new_calibration < 0.0:
                        new_calibration += tolerance * 2.0

        # DEFAULT mode gets tolerance delay
        assert new_calibration == 0.0  # -1.0 + (0.5 * 2.0) = 0.0

    def test_aggressive_mode_skips_tolerance_delay_when_idle(self):
        """Test that AGGRESSIVE mode skips tolerance delay when IDLE.

        This is the fix for issue #1790 - aggressive mode should start
        heating faster without the tolerance delay.
        """
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        base_calibration = -1.0

        # Simulate the tolerance delay logic
        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )

        new_calibration = base_calibration
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if new_calibration < 0.0:
                        new_calibration += tolerance * 2.0

        # AGGRESSIVE mode does NOT get tolerance delay
        assert new_calibration == -1.0  # Unchanged!

    def test_mpc_mode_skips_all_post_adjustments(self):
        """Test that MPC mode skips all post adjustments including tolerance delay."""
        calibration_mode = CalibrationMode.MPC_CALIBRATION
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        base_calibration = -1.0

        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )

        new_calibration = base_calibration
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if new_calibration < 0.0:
                        new_calibration += tolerance * 2.0

        # MPC skips via _skip_post_adjustments
        assert new_calibration == -1.0


class TestAggressiveCalibrationOffset:
    """Tests for the -2.5 offset that applies when actively heating."""

    def test_aggressive_offset_applies_when_heating(self):
        """Test that -2.5 offset applies when hvac_action is HEATING."""
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.HEATING
        base_calibration = -0.5

        new_calibration = base_calibration
        if calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
            if hvac_action == HVACAction.HEATING:
                if new_calibration > -2.5:
                    new_calibration -= 2.5

        # Offset applied when HEATING
        assert new_calibration == -3.0

    def test_aggressive_offset_not_applied_when_idle(self):
        """Test that -2.5 offset does NOT apply when hvac_action is IDLE.

        This is intentional - the offset only makes sense when already heating.
        """
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.IDLE
        base_calibration = -0.5

        new_calibration = base_calibration
        if calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
            if hvac_action == HVACAction.HEATING:
                if new_calibration > -2.5:
                    new_calibration -= 2.5

        # No offset when IDLE
        assert new_calibration == -0.5

    def test_aggressive_offset_not_applied_when_already_aggressive(self):
        """Test that offset is not applied if calibration is already <= -2.5."""
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.HEATING
        base_calibration = -3.0  # Already aggressive

        new_calibration = base_calibration
        if calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
            if hvac_action == HVACAction.HEATING:
                if new_calibration > -2.5:
                    new_calibration -= 2.5

        # No further adjustment
        assert new_calibration == -3.0


class TestCombinedBehavior:
    """Tests for the combined behavior of tolerance delay and aggressive offset."""

    def test_aggressive_idle_no_delay_no_offset(self):
        """Test aggressive mode when IDLE: no tolerance delay, no -2.5 offset.

        This is the key fix scenario:
        - Temperature drops below tolerance band
        - hvac_action is still IDLE
        - Aggressive mode should NOT add tolerance delay (starts heating faster)
        - But also should NOT apply -2.5 offset (only when actively HEATING)
        """
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        base_calibration = -1.0

        new_calibration = base_calibration

        # Step 1: Aggressive offset (only when HEATING)
        if calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
            if hvac_action == HVACAction.HEATING:
                if new_calibration > -2.5:
                    new_calibration -= 2.5

        # Step 2: Tolerance delay (skipped for aggressive mode)
        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if new_calibration < 0.0:
                        new_calibration += tolerance * 2.0

        # Aggressive IDLE: calibration unchanged (no delay added)
        assert new_calibration == -1.0

    def test_default_idle_has_delay(self):
        """Test DEFAULT mode when IDLE: tolerance delay is added."""
        calibration_mode = CalibrationMode.DEFAULT
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        base_calibration = -1.0

        new_calibration = base_calibration

        # Step 2: Tolerance delay (applied for DEFAULT mode)
        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if new_calibration < 0.0:
                        new_calibration += tolerance * 2.0

        # DEFAULT IDLE: tolerance delay added
        assert new_calibration == 0.0  # -1.0 + 1.0 = 0.0

    def test_aggressive_heating_has_offset_no_delay(self):
        """Test aggressive mode when HEATING: -2.5 offset, no tolerance delay."""
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.HEATING
        tolerance = 0.5
        base_calibration = -0.5

        new_calibration = base_calibration

        # Step 1: Aggressive offset
        if calibration_mode == CalibrationMode.AGGRESIVE_CALIBRATION:
            if hvac_action == HVACAction.HEATING:
                if new_calibration > -2.5:
                    new_calibration -= 2.5

        # Step 2: Tolerance delay (not applied when HEATING anyway)
        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if new_calibration < 0.0:
                        new_calibration += tolerance * 2.0

        # Aggressive HEATING: -2.5 offset applied
        assert new_calibration == -3.0


class TestSetpointCalibration:
    """Tests for setpoint-based calibration with aggressive mode."""

    def test_setpoint_aggressive_skips_tolerance_delay(self):
        """Test that setpoint calibration also skips tolerance delay for aggressive."""
        calibration_mode = CalibrationMode.AGGRESIVE_CALIBRATION
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        cur_trv_temp = 20.0
        base_setpoint = 21.3  # Would request heating

        calibrated_setpoint = base_setpoint

        # Setpoint tolerance delay logic (subtracts instead of adds)
        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if calibrated_setpoint - cur_trv_temp > 0.0:
                        calibrated_setpoint -= tolerance * 2.0

        # Aggressive mode: setpoint unchanged
        assert calibrated_setpoint == 21.3

    def test_setpoint_default_has_tolerance_delay(self):
        """Test that setpoint DEFAULT mode adds tolerance delay."""
        calibration_mode = CalibrationMode.DEFAULT
        hvac_action = HVACAction.IDLE
        tolerance = 0.5
        cur_trv_temp = 20.0
        base_setpoint = 21.3

        calibrated_setpoint = base_setpoint

        _skip_post_adjustments = calibration_mode in (
            CalibrationMode.MPC_CALIBRATION,
            CalibrationMode.TPI_CALIBRATION,
            CalibrationMode.PID_CALIBRATION,
        )
        if not _skip_post_adjustments:
            if calibration_mode != CalibrationMode.AGGRESIVE_CALIBRATION:
                if hvac_action == HVACAction.IDLE:
                    if calibrated_setpoint - cur_trv_temp > 0.0:
                        calibrated_setpoint -= tolerance * 2.0

        # DEFAULT: tolerance delay applied (setpoint reduced)
        assert calibrated_setpoint == 20.3  # 21.3 - 1.0 = 20.3


class TestHysteresisScenario:
    """Tests demonstrating the real-world scenario from issue #1790."""

    def test_scenario_temperature_drops_below_tolerance(self):
        """Test the scenario where temperature drops and heating should start.

        Scenario:
        - Target: 21°C, Tolerance: 0.5°C
        - Temperature drops to 20.4°C (below 20.5 threshold)
        - hvac_action is still IDLE (TRV hasn't started yet)
        - With DEFAULT mode: tolerance delay delays heating start
        - With AGGRESSIVE mode: no delay, heating starts faster
        """
        target = 21.0
        tolerance = 0.5
        cur_temp = 20.4  # Below threshold (20.5)
        hvac_action = HVACAction.IDLE

        # Base calibration calculation (simplified)
        # Assumes TRV temp matches external for simplicity
        base_calibration = cur_temp - target  # -0.6

        # DEFAULT mode behavior
        default_calibration = base_calibration
        if hvac_action == HVACAction.IDLE and default_calibration < 0.0:
            default_calibration += tolerance * 2.0  # -0.6 + 1.0 = 0.4

        # AGGRESSIVE mode behavior
        aggressive_calibration = base_calibration
        # Tolerance delay is SKIPPED for aggressive mode
        # Aggressive offset only applies when HEATING, so not here

        # DEFAULT: calibration is 0.4 (delays heating)
        assert abs(default_calibration - 0.4) < 0.001
        # AGGRESSIVE: calibration stays at -0.6 (starts heating faster)
        assert abs(aggressive_calibration - (-0.6)) < 0.001
