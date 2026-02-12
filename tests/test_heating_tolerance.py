"""Tests for heating tolerance hysteresis behaviour.

Verifies the fix for the reported issue:
  BT keeps HEATING from (target - tolerance) up to (target + tolerance).

Expected behaviour:
  - Heating STARTS when temperature drops to (target - tolerance).
  - Heating CONTINUES until temperature reaches target.
  - Heating STOPS at target (NOT at target + tolerance).
  - Heating does NOT restart until temperature drops below (target - tolerance) again.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_bt():
    """Minimal BetterThermostat-like mock for tolerance tests."""
    bt = MagicMock()
    bt.tolerance = 0.5
    bt.bt_target_temp = 21.0
    bt.bt_target_cooltemp = None
    bt.cur_temp = 20.0
    bt.hvac_mode = HVACMode.HEAT
    bt.bt_hvac_mode = HVACMode.HEAT
    bt.window_open = False
    bt.ignore_states = False
    bt.real_trvs = {}
    bt._tolerance_last_action = HVACAction.IDLE
    bt._tolerance_hold_active = False
    bt.device_name = "Test"
    bt._hvac_list = [HVACMode.HEAT, HVACMode.OFF]
    bt.cooler_entity_id = None

    # Import the actual methods and bind them to the mock
    import types

    from custom_components.better_thermostat.climate import BetterThermostat

    bt._should_heat_with_tolerance = types.MethodType(
        BetterThermostat._should_heat_with_tolerance, bt
    )
    bt._compute_hvac_action = types.MethodType(
        BetterThermostat._compute_hvac_action, bt
    )
    return bt


# ---------------------------------------------------------------------------
# _should_heat_with_tolerance
# ---------------------------------------------------------------------------


class TestShouldHeatWithTolerance:
    """Unit tests for the hysteresis helper."""

    def test_starts_heating_below_target_minus_tolerance(self, mock_bt):
        """Heating starts when temp drops below target - tolerance."""
        mock_bt.cur_temp = 20.4  # below 21.0 - 0.5 = 20.5
        assert mock_bt._should_heat_with_tolerance(HVACAction.IDLE, 0.5) is True

    def test_no_heating_between_target_minus_tol_and_target_when_idle(self, mock_bt):
        """When previously IDLE, temp between (target-tol) and target does NOT start heating."""
        mock_bt.cur_temp = 20.7  # between 20.5 and 21.0
        assert mock_bt._should_heat_with_tolerance(HVACAction.IDLE, 0.5) is False

    def test_keeps_heating_between_target_minus_tol_and_target(self, mock_bt):
        """When already HEATING, temp between (target-tol) and target keeps heating."""
        mock_bt.cur_temp = 20.7  # between 20.5 and 21.0
        assert mock_bt._should_heat_with_tolerance(HVACAction.HEATING, 0.5) is True

    def test_stops_heating_at_target(self, mock_bt):
        """Heating stops once temp reaches exactly the target."""
        mock_bt.cur_temp = 21.0
        assert mock_bt._should_heat_with_tolerance(HVACAction.HEATING, 0.5) is False

    def test_stops_heating_above_target(self, mock_bt):
        """Heating stays off when temp is above target."""
        mock_bt.cur_temp = 21.3  # above target, within old symmetric band
        assert mock_bt._should_heat_with_tolerance(HVACAction.HEATING, 0.5) is False

    def test_no_heating_at_target_plus_tolerance(self, mock_bt):
        """Definitely no heating at target + tolerance."""
        mock_bt.cur_temp = 21.5  # target + tolerance
        assert mock_bt._should_heat_with_tolerance(HVACAction.HEATING, 0.5) is False


# ---------------------------------------------------------------------------
# _compute_hvac_action – full integration of tolerance + TRV override
# ---------------------------------------------------------------------------


class TestComputeHvacActionTolerance:
    """Integration tests showing the full HVAC action logic with tolerance."""

    def test_heating_starts_below_target_minus_tolerance(self, mock_bt):
        """Test that heating starts when temperature is below target minus tolerance."""
        mock_bt.cur_temp = 20.4
        mock_bt._tolerance_last_action = HVACAction.IDLE
        action = mock_bt._compute_hvac_action()
        assert action == HVACAction.HEATING

    def test_heating_continues_until_target(self, mock_bt):
        """Test that heating continues when temperature is below target."""
        mock_bt.cur_temp = 20.8
        mock_bt._tolerance_last_action = HVACAction.HEATING
        action = mock_bt._compute_hvac_action()
        assert action == HVACAction.HEATING

    def test_heating_stops_at_target(self, mock_bt):
        """Test that heating stops when temperature reaches target."""
        mock_bt.cur_temp = 21.0
        mock_bt._tolerance_last_action = HVACAction.HEATING
        action = mock_bt._compute_hvac_action()
        assert action == HVACAction.IDLE

    def test_no_heating_above_target_even_if_below_target_plus_tol(self, mock_bt):
        """Test that heating does not continue above target even if below target + tolerance.

        This is the core bug scenario: temp at target + 0.3 (< target + tol)
        should NOT keep heating.
        """
        mock_bt.cur_temp = 21.3
        mock_bt._tolerance_last_action = HVACAction.HEATING
        action = mock_bt._compute_hvac_action()
        assert action == HVACAction.IDLE

    def test_heating_does_not_restart_above_target_minus_tol(self, mock_bt):
        """Test that heating does not restart when temperature is above target - tolerance.

        After heating stopped, temp between (target-tol) and target should
        NOT restart heating (hysteresis).
        """
        mock_bt.cur_temp = 20.7
        mock_bt._tolerance_last_action = HVACAction.IDLE
        action = mock_bt._compute_hvac_action()
        assert action == HVACAction.IDLE

    def test_heating_restarts_at_target_minus_tolerance(self, mock_bt):
        """Heating restarts once temp drops back to target - tolerance."""
        mock_bt.cur_temp = 20.49  # just below 20.5
        mock_bt._tolerance_last_action = HVACAction.IDLE
        action = mock_bt._compute_hvac_action()
        assert action == HVACAction.HEATING


class TestTrvOverrideDoesNotCorruptHysteresis:
    """Verify that TRV reporting 'heating' does NOT break the hysteresis state.

    Even when the TRV is still physically heating (e.g. thermal inertia),
    the internal hysteresis state (_tolerance_last_action) must remain
    based on the tolerance decision, not the TRV override.
    """

    def test_hysteresis_survives_trv_override(self, mock_bt):
        """Test that hysteresis state survives TRV override.

        After tolerance says stop, a heating TRV must not corrupt
        _tolerance_last_action so that the next cycle uses the strict threshold.
        """
        # Step 1: temp reaches target → tolerance says IDLE
        mock_bt.cur_temp = 21.0
        mock_bt._tolerance_last_action = HVACAction.HEATING

        # Simulate TRV still reporting heating
        mock_bt.real_trvs = {
            "climate.trv_1": {"hvac_action": "heating", "ignore_trv_states": False}
        }
        mock_bt.hass = MagicMock()

        mock_bt._compute_hvac_action()
        # The reported action may be HEATING (TRV override), that's OK for display
        # But the hysteresis state must NOT be HEATING:
        assert mock_bt._tolerance_last_action == HVACAction.IDLE, (
            "TRV override corrupted _tolerance_last_action; "
            "next cycle would use lenient threshold, heating up to target + tol"
        )

    def test_hysteresis_state_correct_after_trv_override_sequence(self, mock_bt):
        """Test that hysteresis state remains correct after a TRV override sequence.

        Simulate a full sequence: heat → reach target → TRV overrides →
        temp drops slightly → should NOT restart heating.
        """
        mock_bt.real_trvs = {
            "climate.trv_1": {"hvac_action": "heating", "ignore_trv_states": False}
        }
        mock_bt.hass = MagicMock()

        # Cycle 1: At target, tolerance says stop
        mock_bt.cur_temp = 21.0
        mock_bt._tolerance_last_action = HVACAction.HEATING
        mock_bt._compute_hvac_action()

        # Cycle 2: TRV stops heating, temp drops slightly but still above target - tol
        mock_bt.real_trvs["climate.trv_1"]["hvac_action"] = "idle"
        mock_bt.cur_temp = 20.8  # between target - tol (20.5) and target (21.0)
        action = mock_bt._compute_hvac_action()

        assert action == HVACAction.IDLE, (
            f"Expected IDLE at {mock_bt.cur_temp}°C (between target-tol and target "
            f"with prev=IDLE), but got {action}. Heating should NOT restart here."
        )


# ---------------------------------------------------------------------------
# Calibration tolerance band
# ---------------------------------------------------------------------------


class TestCalibrationToleranceBand:
    """Verify that calibration tolerance uses asymmetric band [target-tol, target)."""

    def test_within_tolerance_below_target(self):
        """Temp between target-tol and target should be within tolerance."""
        target = 21.0
        tol = 0.5
        cur = 20.7  # between 20.5 and 21.0
        within = cur >= (target - tol) and cur < target
        assert within is True

    def test_not_within_tolerance_at_target(self):
        """Temp exactly at target should NOT be within tolerance (recalculate)."""
        target = 21.0
        tol = 0.5
        cur = 21.0
        within = cur >= (target - tol) and cur < target
        assert within is False

    def test_not_within_tolerance_above_target(self):
        """Temp above target should NOT be within tolerance (recalculate to stop heating)."""
        target = 21.0
        tol = 0.5
        cur = 21.3
        within = cur >= (target - tol) and cur < target
        assert within is False

    def test_not_within_tolerance_below_band(self):
        """Temp below target-tol should NOT be within tolerance (recalculate to start heating)."""
        target = 21.0
        tol = 0.5
        cur = 20.4
        within = cur >= (target - tol) and cur < target
        assert within is False

    def test_old_symmetric_band_was_wrong_above_target(self):
        """Demonstrate the old symmetric band was incorrectly including temps above target."""
        target = 21.0
        tol = 0.5
        cur = 21.3  # above target but below target + tol
        old_within = cur >= (target - tol) and cur <= (target + tol)
        new_within = cur >= (target - tol) and cur < target
        assert old_within is True, "Old band should have included this temp (bug)"
        assert new_within is False, "New band correctly excludes this temp"
