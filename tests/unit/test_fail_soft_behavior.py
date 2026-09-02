"""Behavior tests for the fail-soft ladder's effect on the control law.

SENSOR_FALLBACK substitutes the mean of the available TRV-internal
temperatures for a dead room sensor; HOLD stops adjusting entirely; one
dead TRV never drags the others down; and the watchdog flags a silently
stalled control loop.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_setpoint,
    effective_room_temp,
)
from custom_components.better_thermostat.core.decide import (
    KernelState,
    decide,
    running_kernel_state,
)
from custom_components.better_thermostat.core.fsm.control_mode import (
    ControlMode,
    ControlModeState,
)
from custom_components.better_thermostat.core.snapshot import (
    HvacMode,
    TrvReported,
    WorldSnapshot,
)
from custom_components.better_thermostat.core.watchdog import control_loop_stalled
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.const import CalibrationMode


def _bt(mode: ControlMode) -> MagicMock:
    bt = MagicMock()
    bt.cur_temp = 20.0
    bt.kernel_state = KernelState(control_mode=ControlModeState(mode=mode))
    bt.real_trvs = {
        "climate.a": Trv.from_legacy_dict("climate.a", {"current_temperature": 21.0}),
        "climate.b": Trv.from_legacy_dict("climate.b", {"current_temperature": 23.0}),
    }
    return bt


class TestSensorFallbackSubstitution:
    """SENSOR_FALLBACK calibrates on the TRV-internal temperatures."""

    def test_optimal_uses_the_room_sensor(self):
        """On OPTIMAL the room sensor value is used unchanged."""
        assert effective_room_temp(_bt(ControlMode.OPTIMAL)) == 20.0

    def test_fallback_uses_the_trv_mean(self):
        """On SENSOR_FALLBACK the mean of the TRV temperatures substitutes."""
        assert effective_room_temp(_bt(ControlMode.SENSOR_FALLBACK)) == 22.0

    def test_fallback_without_trv_temps_keeps_the_last_reading(self):
        """Without any TRV temperature the (stale) room reading remains."""
        bt = _bt(ControlMode.SENSOR_FALLBACK)
        for trv in bt.real_trvs.values():
            trv.current_temperature = None
        assert effective_room_temp(bt) == 20.0

    def test_hold_does_not_substitute(self):
        """HOLD does not fabricate temperatures; the controller pauses."""
        assert effective_room_temp(_bt(ControlMode.HOLD)) == 20.0


class TestFallbackSetpointChannel:
    """The setpoint channel uses the fallback temperature verbatim."""

    def test_zero_degree_fallback_reading_is_used(self):
        """A TRV mean of exactly 0.0 °C is a reading, not a missing value.

        The stale room-sensor value must not silently substitute for it.
        """
        quirks = MagicMock()
        quirks.fix_target_temperature_calibration.side_effect = (
            lambda _self, _eid, temperature: float(temperature)
        )
        bt = MagicMock()
        bt.name = "better_thermostat"
        bt.device_name = "Test BT"
        bt.tolerance = 0.0
        bt.hvac_action = HVACAction.HEATING
        bt.cur_temp = 18.0  # stale reading from the dead room sensor
        bt.bt_target_temp = 5.0
        bt.kernel_state = KernelState(
            control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)
        )
        bt.real_trvs = {
            "climate.a": Trv.from_legacy_dict(
                "climate.a",
                {
                    "advanced": {"calibration_mode": CalibrationMode.DEFAULT},
                    "current_temperature": 4.0,
                    "target_temp_step": 0.5,
                    "min_temp": 5.0,
                    "max_temp": 30.0,
                    "model_quirks": quirks,
                },
            ),
            "climate.b": Trv.from_legacy_dict(
                "climate.b", {"current_temperature": -4.0}
            ),
        }

        result = calculate_calibration_setpoint(bt, "climate.a")

        # (target 5.0 - fallback mean 0.0) + TRV temp 4.0 = 9.0
        assert result == pytest.approx(9.0)


class TestBulkhead:
    """One dead TRV never drags the others down (per-TRV isolation)."""

    def test_one_offline_trv_leaves_the_other_heating(self):
        """The reachable TRV keeps its heating intent."""
        snapshot = WorldSnapshot(
            now=datetime(2026, 1, 10, tzinfo=UTC),
            now_monotonic=1000.0,
            target_temp=21.0,
            hvac_mode=HvacMode.HEAT,
            room_temp=19.0,
            call_for_heat=True,
            trvs={
                "climate.ok": TrvReported(entity_id="climate.ok", available=True),
                "climate.dead": TrvReported(entity_id="climate.dead", available=False),
            },
        )
        desired, state = decide(snapshot, running_kernel_state())
        assert set(desired.trvs) == {"climate.ok"}
        assert desired.trvs["climate.ok"].hvac_mode == HvacMode.HEAT
        assert state.reachability["climate.dead"].online is False


class TestWatchdog:
    """The watchdog answers whether a control cycle completed recently."""

    def test_never_ran_is_not_a_stall(self):
        """Before the first cycle the watchdog stays quiet (startup gate)."""
        assert control_loop_stalled(None, now=10_000.0) is False

    def test_recent_cycle_is_fine(self):
        """A cycle within the window is healthy."""
        assert control_loop_stalled(9_500.0, now=10_000.0) is False

    def test_stale_cycle_raises_the_alarm(self):
        """No cycle for longer than the window flags a stall."""
        assert control_loop_stalled(1_000.0, now=10_000.0) is True
