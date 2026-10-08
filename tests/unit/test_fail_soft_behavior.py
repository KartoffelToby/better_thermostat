"""Behavior tests for the fail-soft ladder's effect on the control law.

SENSOR_FALLBACK substitutes the mean of the available TRV-internal
temperatures for a dead room sensor; HOLD stops adjusting entirely; one
dead TRV never drags the others down; and the watchdog flags a silently
stalled control loop.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction
from homeassistant.core import State
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
from custom_components.better_thermostat.utils.const import CalibrationMode
from tests.factories import ThermostatStandIn, trv_from_legacy_dict


def _bt(mode: ControlMode) -> MagicMock:
    bt = ThermostatStandIn()
    bt.device_name = "Test BT"
    bt.room_temperature = 20.0
    bt.kernel_state = KernelState(control_mode=ControlModeState(mode=mode))
    bt.real_trvs = {
        "climate.a": trv_from_legacy_dict("climate.a", {"current_temperature": 21.0}),
        "climate.b": trv_from_legacy_dict("climate.b", {"current_temperature": 23.0}),
    }
    _publish(bt, {"climate.a": 21.0, "climate.b": 23.0})
    return bt


def _publish(bt: MagicMock, reported: dict[str, object]) -> None:
    """Let each TRV report ``heat`` with the internal temperature given for it."""

    def _state(entity_id: str) -> State:
        value = reported.get(entity_id)
        attributes = {} if value is None else {"current_temperature": value}
        return State(entity_id, "heat", attributes)

    bt.hass.states.get.side_effect = _state
    bt.hass.config.units.temperature_unit = "°C"


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

    def test_fallback_leaves_out_an_unreachable_trv(self):
        """Only TRVs that are reachable contribute to the substitute.

        A reading stored before its TRV went unavailable no longer describes
        the room, even when nothing has cleared it yet.
        """
        bt = _bt(ControlMode.SENSOR_FALLBACK)
        bt.hass.states.get.side_effect = lambda entity_id: (
            State(entity_id, "unavailable")
            if entity_id == "climate.b"
            else State(entity_id, "heat", {"current_temperature": 21.0})
        )
        assert effective_room_temp(bt) == 21.0

    def test_fallback_with_every_trv_unreachable_keeps_the_last_reading(self):
        """Stored readings of unreachable TRVs do not replace the room reading."""
        bt = _bt(ControlMode.SENSOR_FALLBACK)
        bt.hass.states.get.side_effect = lambda entity_id: None
        assert effective_room_temp(bt) == 20.0

    def test_hold_does_not_substitute(self):
        """HOLD does not fabricate temperatures; the controller pauses."""
        assert effective_room_temp(_bt(ControlMode.HOLD)) == 20.0

    @pytest.mark.parametrize(
        "reported",
        [
            pytest.param(126.5, id="marker"),
            pytest.param(-60.0, id="implausible"),
            pytest.param("not a number", id="non_numeric"),
            pytest.param(None, id="no_reading"),
        ],
    )
    def test_fallback_leaves_out_a_trv_that_reports_no_usable_temperature(
        self, reported
    ):
        """A TRV speaks for the room only on a temperature it reports now.

        The handler keeps the stored reading across a report it cannot use,
        so a device that goes on reporting a marker or garbage would
        otherwise stand in for the room with a value it no longer confirms.
        """
        bt = _bt(ControlMode.SENSOR_FALLBACK)
        _publish(bt, {"climate.a": 21.0, "climate.b": reported})
        assert effective_room_temp(bt) == 21.0


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
        bt = ThermostatStandIn()
        bt.name = "better_thermostat"
        bt.device_name = "Test BT"
        bt.tolerance = 0.0
        bt.hvac_action = HVACAction.HEATING
        bt.room_temperature = 18.0  # stale reading from the dead room sensor
        bt.heat_target_temperature = 5.0
        bt.kernel_state = KernelState(
            control_mode=ControlModeState(mode=ControlMode.SENSOR_FALLBACK)
        )
        bt.real_trvs = {
            "climate.a": trv_from_legacy_dict(
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
            "climate.b": trv_from_legacy_dict(
                "climate.b", {"current_temperature": -4.0}
            ),
        }

        _publish(bt, {"climate.a": 4.0, "climate.b": -4.0})

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
            heat_target_temperature=21.0,
            hvac_mode=HvacMode.HEAT,
            room_temperature=19.0,
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
