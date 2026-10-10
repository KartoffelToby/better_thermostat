"""Tests for calibration mode behaviors (aggressive, default, MPC, etc.).

Integration tests that invoke the real production functions
``calculate_calibration_local()`` and ``calculate_calibration_setpoint()``
instead of reimplementing their arithmetic inline.

Verifies:
- Tolerance-delay post-adjustment is skipped for DEFAULT, AGGRESSIVE, MPC,
  TPI, and PID modes (each for its own reason).
- The aggressive -2.5 local-calibration / +2.5 setpoint offset is applied
  only when ``hvac_action == HEATING`` and the value hasn't already exceeded
  the threshold.
- Real-world hysteresis scenario from issue #1790.
"""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.calibration.pid import PIDState
from custom_components.better_thermostat.utils.const import CalibrationMode
from tests.factories import ThermostatStandIn, make_state

ENTITY_ID = "climate.test_trv"


def _make_bt(
    calibration_mode,
    hvac_action,
    room_temperature=20.0,
    heat_target_temperature=21.0,
    tolerance=0.5,
    trv_temperature=21.0,
    last_calibration=0.0,
    calibration_step=0.1,
    minimum_calibration=-5.0,
    cal_max=5.0,
    target_temp_step=0.1,
    min_temp=5.0,
    max_temp=30.0,
):
    """Build a minimal mock BetterThermostat instance for calibration tests.

    ``bt_hvac_mode`` is set to ``HVACMode.OFF`` so that MPC / TPI / PID
    internal compute helpers short-circuit immediately (they bail out when
    the HVAC mode is OFF) and the test only exercises offset / post-adjustment
    logic.
    """
    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = tolerance
    bt.attr_hvac_action = hvac_action
    bt.hvac_action = hvac_action
    bt.room_temperature = room_temperature
    bt.room_temperature_filtered = None
    bt.heat_target_temperature = heat_target_temperature
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.contact_open = False
    # Short-circuit MPC/TPI/PID internal compute
    bt.bt_hvac_mode = HVACMode.OFF
    # PID standby still follows the room, on a real state and clock.
    bt.clock = FakeClock(monotonic_value=1_000.0)
    bt.state_mgr = MagicMock()
    bt.state_mgr.get_pid.side_effect = lambda _key: PIDState()
    pid_entries: dict[str, PIDState] = {}
    bt.state_mgr.state.pid = pid_entries

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _eid, calibration_offset: (
        float(calibration_offset)
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _eid, temperature: float(temperature)
    )

    bt.real_trvs = {
        ENTITY_ID: Trv(
            entity_id=ENTITY_ID,
            advanced={
                "calibration_mode": calibration_mode,
                "protect_overheating": False,
            },
            current_temperature=trv_temperature,
            last_calibration=last_calibration,
            local_calibration_step=calibration_step,
            min_local_calibration=minimum_calibration,
            max_local_calibration=cal_max,
            target_temp_step=target_temp_step,
            min_temp=min_temp,
            max_temp=max_temp,
            model_quirks=quirks,
        )
    }
    return bt


# ---------------------------------------------------------------------------
# Local-calibration tolerance-delay behaviour
# ---------------------------------------------------------------------------
class TestToleranceDelayBehavior:
    """Tolerance-delay post-adjustment is skipped for DEFAULT, AGGRESSIVE, and MPC/TPI/PID modes.

    Each mode skips through a different code-path.
    With the mock inputs (room_temperature=20.0, trv_temperature=21.0, last_calibration=0.0)
    the base calibration is ``(20.0 − 21.0) + 0.0 = −1.0``.  All three modes
    return that value unchanged because no post-adjustments fire.
    """

    def test_default_mode_skips_tolerance_delay_when_idle(self):
        """DEFAULT is in ``_skip_post_adjustments`` → no tolerance delay."""
        bt = _make_bt(CalibrationMode.DEFAULT, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-1.0)

    def test_aggressive_mode_skips_tolerance_delay_when_idle(self):
        """AGGRESSIVE is excluded by an explicit ``!=`` check → no delay."""
        bt = _make_bt(CalibrationMode.AGGRESSIVE_CALIBRATION, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-1.0)

    def test_mpc_mode_skips_all_post_adjustments(self):
        """MPC is in ``_skip_post_adjustments`` → no tolerance delay."""
        bt = _make_bt(CalibrationMode.MPC_CALIBRATION, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-1.0)

    def test_tpi_mode_skips_all_post_adjustments(self):
        """TPI is in ``_skip_post_adjustments`` → no tolerance delay."""
        bt = _make_bt(CalibrationMode.TPI_CALIBRATION, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-1.0)

    def test_pid_mode_skips_all_post_adjustments(self):
        """PID is in ``_skip_post_adjustments`` → no tolerance delay."""
        bt = _make_bt(CalibrationMode.PID_CALIBRATION, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# Aggressive -2.5 local-calibration offset
# ---------------------------------------------------------------------------
class TestAggressiveCalibrationOffset:
    """The -2.5 offset only fires for AGGRESSIVE + HEATING + cal > -2.5.

    Mock inputs use ``room_temperature=20.5, heat_target_temperature=22.0`` (outside tolerance)
    and ``trv_temperature=21.0, last_calibration=0.0`` to produce a base calibration
    of ``(20.5 − 21.0) + 0.0 = −0.5``.
    """

    def test_aggressive_offset_applies_when_heating(self):
        """HEATING + cal > -2.5 → cal becomes -0.5 − 2.5 = -3.0."""
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.HEATING,
            room_temperature=20.5,
            heat_target_temperature=22.0,
        )
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-3.0)

    def test_aggressive_offset_not_applied_when_idle(self):
        """IDLE → no -2.5 offset; calibration stays at base (-0.5)."""
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.IDLE,
            room_temperature=20.5,
            heat_target_temperature=22.0,
        )
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-0.5)

    def test_aggressive_offset_not_applied_when_already_aggressive(self):
        """Base cal already ≤ -2.5 → no further adjustment.

        room_temperature=18.0 → base = (18.0 − 21.0) + 0.0 = -3.0.
        """
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.HEATING,
            room_temperature=18.0,
            heat_target_temperature=22.0,
        )
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# Combined tolerance-delay + aggressive-offset
# ---------------------------------------------------------------------------
class TestCombinedBehavior:
    """End-to-end combinations of calibration mode × hvac_action."""

    def test_aggressive_idle_no_delay_no_offset(self):
        """AGGRESSIVE + IDLE → neither tolerance delay nor -2.5 offset."""
        bt = _make_bt(CalibrationMode.AGGRESSIVE_CALIBRATION, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        # base: (20.0 - 21.0) + 0.0 = -1.0, unchanged
        assert result == pytest.approx(-1.0)

    def test_default_idle_skips_delay(self):
        """DEFAULT + IDLE → post-adjustments skipped, calibration is base."""
        bt = _make_bt(CalibrationMode.DEFAULT, HVACAction.IDLE)
        result = calculate_calibration_local(bt, ENTITY_ID)
        assert result == pytest.approx(-1.0)

    def test_aggressive_heating_has_offset_no_delay(self):
        """AGGRESSIVE + HEATING → -2.5 offset applied, no tolerance delay."""
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.HEATING,
            room_temperature=20.5,
            heat_target_temperature=22.0,
        )
        result = calculate_calibration_local(bt, ENTITY_ID)
        # base -0.5 − 2.5 = -3.0
        assert result == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# Setpoint calibration
# ---------------------------------------------------------------------------
class TestSetpointCalibration:
    """Setpoint calibration: ``(target − external) + trv_temperature``.

    Mock inputs: ``heat_target_temperature=21.3, room_temperature=20.0, trv_temperature=20.0``
    → base setpoint = ``(21.3 − 20.0) + 20.0 = 21.3``.
    """

    def test_setpoint_aggressive_skips_tolerance_delay(self):
        """AGGRESSIVE setpoint is not reduced by tolerance delay."""
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.IDLE,
            heat_target_temperature=21.3,
            room_temperature=20.0,
            trv_temperature=20.0,
        )
        result = calculate_calibration_setpoint(bt, ENTITY_ID)
        assert result == pytest.approx(21.3)

    def test_setpoint_default_skips_tolerance_delay(self):
        """DEFAULT setpoint: post-adjustments skipped (DEFAULT in skip set)."""
        bt = _make_bt(
            CalibrationMode.DEFAULT,
            HVACAction.IDLE,
            heat_target_temperature=21.3,
            room_temperature=20.0,
            trv_temperature=20.0,
        )
        result = calculate_calibration_setpoint(bt, ENTITY_ID)
        assert result == pytest.approx(21.3)

    def test_setpoint_aggressive_offset_when_heating(self):
        """AGGRESSIVE + HEATING → +2.5 added when gap < 2.5."""
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.HEATING,
            heat_target_temperature=22.0,
            room_temperature=20.0,
            trv_temperature=20.0,
        )
        # base setpoint = (22.0 - 20.0) + 20.0 = 22.0
        # gap = 22.0 - 20.0 = 2.0 < 2.5 → setpoint += 2.5 → 24.5
        result = calculate_calibration_setpoint(bt, ENTITY_ID)
        assert result == pytest.approx(24.5)

    @pytest.mark.parametrize(
        ("heat_target_temperature", "expected"), [(22.4, 24.9), (22.5, 22.5)]
    )
    def test_setpoint_aggressive_boost_jumps_at_the_threshold(
        self, heat_target_temperature, expected
    ):
        """The boost adds a full 2.5 just below the threshold and nothing at it."""
        bt = _make_bt(
            CalibrationMode.AGGRESSIVE_CALIBRATION,
            HVACAction.HEATING,
            heat_target_temperature=heat_target_temperature,
            room_temperature=20.0,
            trv_temperature=20.0,
        )
        result = calculate_calibration_setpoint(bt, ENTITY_ID)
        assert result == pytest.approx(expected)

    def test_setpoint_mpc_skips_post_adjustments(self):
        """MPC setpoint: post-adjustments skipped."""
        bt = _make_bt(
            CalibrationMode.MPC_CALIBRATION,
            HVACAction.IDLE,
            heat_target_temperature=21.3,
            room_temperature=20.0,
            trv_temperature=20.0,
        )
        result = calculate_calibration_setpoint(bt, ENTITY_ID)
        # MPC compute short-circuits (hvac_mode OFF), base setpoint stays
        assert result == pytest.approx(21.3)


# ---------------------------------------------------------------------------
# Real-world hysteresis scenario (issue #1790)
# ---------------------------------------------------------------------------
class TestHysteresisScenario:
    """Scenario from issue #1790: temperature drops below tolerance band while IDLE."""

    def test_scenario_temperature_drops_below_tolerance(self):
        """Both DEFAULT and AGGRESSIVE produce the same base calibration.

        Scenario:
        - Target: 21 °C, Tolerance: 0.5 °C
        - Temperature drops to 20.4 °C (below the 20.5 threshold)
        - hvac_action is still IDLE (TRV hasn't started heating yet)
        - Neither mode applies a tolerance delay in production, so both
          return the base calibration of (20.4 − 21.0) + 0.0 = −0.6.
        """
        common = {
            "room_temperature": 20.4,
            "heat_target_temperature": 21.0,
            "tolerance": 0.5,
            "trv_temperature": 21.0,
            "last_calibration": 0.0,
        }

        default_result = calculate_calibration_local(
            _make_bt(CalibrationMode.DEFAULT, HVACAction.IDLE, **common), ENTITY_ID
        )
        aggressive_result = calculate_calibration_local(
            _make_bt(CalibrationMode.AGGRESSIVE_CALIBRATION, HVACAction.IDLE, **common),
            ENTITY_ID,
        )

        assert default_result == pytest.approx(-0.6, abs=0.001)
        assert aggressive_result == pytest.approx(-0.6, abs=0.001)
