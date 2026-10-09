"""What the two calibration channels send when a reading or a mode is missing.

The local channel writes an offset, the setpoint channel a target. Both
need a room temperature and the TRV's own reading; without one there is
nothing to calibrate on and nothing is sent. A controller that drives the
valve directly keeps the channel out of its way, and keeps driving the valve
while the TRV reports no reading of its own.
"""

from unittest.mock import MagicMock, patch

from homeassistant.components.climate.const import HVACAction, HVACMode
import pytest

from custom_components.better_thermostat.calibration import (
    calculate_calibration_local,
    calculate_calibration_setpoint,
)
from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.utils.const import (
    DEFAULT_CALIBRATION_MODE,
    CalibrationMode,
)
from tests.factories import ThermostatStandIn, make_state, trv_from_legacy_dict

ENTITY_ID = "climate.test_trv"
_CAL = "custom_components.better_thermostat.calibration"


def _make_bt(
    mode=CalibrationMode.DEFAULT,
    *,
    room_temperature: float | None = 18.0,
    heat_target_temperature: float | None = 21.0,
    trv_temperature: float | None = 20.0,
    last_calibration: float = 1.5,
):
    """Return a heating thermostat with one TRV calibrated in ``mode``."""
    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.clock = FakeClock()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = 0.3
    bt.hvac_action = HVACAction.HEATING
    bt.room_temperature = room_temperature
    bt.heat_target_temperature = heat_target_temperature
    bt.outdoor_sensor_entity_id = None
    bt.weather_entity_id = None
    bt.bt_hvac_mode = HVACMode.HEAT

    quirks = MagicMock()
    quirks.fix_local_calibration.side_effect = lambda _self, _eid, calibration_offset: (
        float(calibration_offset)
    )
    quirks.fix_target_temperature_calibration.side_effect = (
        lambda _self, _eid, temperature: float(temperature)
    )
    bt.real_trvs = {
        ENTITY_ID: trv_from_legacy_dict(
            ENTITY_ID,
            {
                "advanced": {"calibration_mode": mode, "protect_overheating": False},
                "current_temperature": trv_temperature,
                "last_calibration": last_calibration,
                "local_calibration_step": 0.1,
                "min_local_calibration": -5.0,
                "max_local_calibration": 5.0,
                "target_temp_step": 0.5,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def _controller_reports(percent: float, *, drives_the_valve: bool):
    """Patch the TRV's controller to report ``percent`` as its valve command."""
    calibrator = MagicMock()
    calibrator.cached.return_value = (percent, drives_the_valve)
    return patch(f"{_CAL}._balance_calibrator", return_value=calibrator)


@pytest.mark.parametrize(
    "calculate", [calculate_calibration_local, calculate_calibration_setpoint]
)
def test_without_a_room_temperature_nothing_is_sent(calculate):
    assert calculate(_make_bt(room_temperature=None), ENTITY_ID) is None


def test_the_setpoint_channel_without_a_target_sends_nothing():
    bt = _make_bt(heat_target_temperature=None)

    assert calculate_calibration_setpoint(bt, ENTITY_ID) is None


def test_the_local_channel_without_a_trv_reading_sends_nothing(caplog):
    """An offset is the gap between two readings; with one missing it is unknown."""
    bt = _make_bt(trv_temperature=None)

    assert calculate_calibration_local(bt, ENTITY_ID) is None
    assert "Could not calculate local calibration" in caplog.text


@pytest.mark.parametrize(
    "mode",
    [
        CalibrationMode.MPC_CALIBRATION,
        CalibrationMode.TPI_CALIBRATION,
        CalibrationMode.PID_CALIBRATION,
    ],
)
def test_the_local_channel_holds_its_offset_while_a_controller_drives_the_valve(mode):
    """The valve command does the heating, so the offset is left where it is."""
    bt = _make_bt(mode, last_calibration=1.5)

    with _controller_reports(40.0, drives_the_valve=True):
        calibration_offset = calculate_calibration_local(bt, ENTITY_ID)

    assert calibration_offset == pytest.approx(1.5)


def test_the_setpoint_channel_keeps_a_closed_valve_from_heating_on_its_own():
    """With the valve commanded shut, the target drops below the TRV's reading.

    The valve command alone does not stop a TRV that also regulates on its
    own setpoint; a target below what it reads keeps it from opening again.
    """
    bt = _make_bt(CalibrationMode.TPI_CALIBRATION, trv_temperature=20.0)

    with _controller_reports(0.0, drives_the_valve=True):
        setpoint = calculate_calibration_setpoint(bt, ENTITY_ID)

    assert setpoint is not None
    assert setpoint < 20.0


@pytest.mark.parametrize(
    "mode",
    [
        CalibrationMode.MPC_CALIBRATION,
        CalibrationMode.MPC_V2_CALIBRATION,
        CalibrationMode.TPI_CALIBRATION,
        CalibrationMode.PID_CALIBRATION,
    ],
)
def test_the_setpoint_channel_without_a_trv_reading_still_runs_the_controller(mode):
    """The controller sizes the valve from the room, so it runs every cycle.

    Only the setpoint is derived from the TRV's own reading; without that
    reading the setpoint is not sent, but the valve intent stays current.
    """
    bt = _make_bt(mode, trv_temperature=None)

    with _controller_reports(0.0, drives_the_valve=True) as balance_calibrator:
        setpoint = calculate_calibration_setpoint(bt, ENTITY_ID)

    assert setpoint is None
    balance_calibrator.return_value.observe.assert_called_once()


def test_the_setpoint_channel_without_a_trv_reading_closes_a_heating_power_valve():
    """A valve opened while the room was cold closes once the room is warm.

    Heating power publishes the valve intent from the room's state, and a
    TRV that reports no reading of its own does not freeze the last one.
    """
    bt = _make_bt(
        CalibrationMode.HEATING_POWER_CALIBRATION,
        room_temperature=23.0,
        trv_temperature=None,
    )
    bt.hvac_action = HVACAction.IDLE
    trv = bt.real_trvs[ENTITY_ID]
    trv.calibration_balance = {"valve_percent": 100, "apply_valve": True}

    with patch(f"{_CAL}._supports_direct_valve_control", return_value=True):
        setpoint = calculate_calibration_setpoint(bt, ENTITY_ID)

    assert setpoint is None
    assert trv.calibration_balance is not None
    assert trv.calibration_balance["valve_percent"] == 0
    assert trv.calibration_balance["apply_valve"] is True


@pytest.mark.parametrize("stored", [None, 3], ids=["none", "legacy_number"])
def test_the_setpoint_channel_reads_an_unusable_mode_as_the_default_mode(stored):
    """A mode stored as nothing or as an old numeric code calibrates as the default.

    Older versions stored the mode as a number, and only ``0`` still names one.
    """
    results = {}
    for mode in (stored, DEFAULT_CALIBRATION_MODE, CalibrationMode.DEFAULT):
        bt = _make_bt(mode)
        bt.heating_power = 0.01
        results[mode] = calculate_calibration_setpoint(bt, ENTITY_ID)

    assert results[DEFAULT_CALIBRATION_MODE] != results[CalibrationMode.DEFAULT]
    assert results[stored] == results[DEFAULT_CALIBRATION_MODE]
