"""The Eurotronic Comet Zigbee (COZB0001) offsets its setpoint, not its reading.

The device regulates on ``reading - (setpoint + offset)`` and reports the
reading without the offset. A device that offsets its reading regulates on
``(reading + offset) - setpoint`` and reports the sum. The local calibration
has to land the COZB0001 on the room temperature all the same, and has to
stay there from one control cycle to the next.
"""

from __future__ import annotations

from types import ModuleType

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.calibration import calculate_calibration_local
from custom_components.better_thermostat.model_fixes import COZB0001, default
from custom_components.better_thermostat.model_fixes.model_quirks import (
    local_calibration_shifts_setpoint,
)
from custom_components.better_thermostat.utils.const import (
    CONF_PROTECT_OVERHEATING,
    CalibrationMode,
)
from tests.factories import ThermostatStandIn, make_state, trv_from_legacy_dict

ENTITY_ID = "climate.comet"
ROOM = 20.0
READING = 22.0


def _host(
    *,
    step: float = 0.1,
    protect_overheating: bool = False,
    mode: CalibrationMode = CalibrationMode.DEFAULT,
    quirks: ModuleType | None = COZB0001,
) -> ThermostatStandIn:
    bt = ThermostatStandIn()
    bt.kernel_state = make_state()
    bt.name = "better_thermostat"
    bt.device_name = "Test BT"
    bt.tolerance = 0.5
    bt.attr_hvac_action = None
    bt.hvac_action = None
    bt.room_temperature = ROOM
    bt.heat_target_temperature = None
    bt.real_trvs = {
        ENTITY_ID: trv_from_legacy_dict(
            ENTITY_ID,
            {
                "advanced": {
                    "calibration_mode": mode,
                    CONF_PROTECT_OVERHEATING: protect_overheating,
                },
                "current_temperature": READING,
                "last_calibration": 0.0,
                "local_calibration_step": step,
                "local_calibration_min": -2.5,
                "local_calibration_max": 2.5,
                "target_temp_step": 0.5,
                "min_temp": 5.0,
                "max_temp": 30.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def _calibrate(bt: ThermostatStandIn) -> float:
    calibration_offset = calculate_calibration_local(bt, ENTITY_ID)
    assert calibration_offset is not None
    return calibration_offset


def _regulated_reading(device_offset: float) -> float:
    """The temperature the COZB0001 compares against its plain setpoint."""
    return READING - device_offset


class TestTheCometIsDeclaredASetpointShifter:
    def test_the_comet_answers_that_it_shifts_its_setpoint(self):
        assert local_calibration_shifts_setpoint(_host(), ENTITY_ID) is True

    def test_a_module_without_the_answer_offsets_its_reading(self):
        bt = _host(quirks=default)
        assert local_calibration_shifts_setpoint(bt, ENTITY_ID) is False

    def test_a_trv_without_a_loaded_module_offsets_its_reading(self):
        bt = _host(quirks=None)
        assert local_calibration_shifts_setpoint(bt, ENTITY_ID) is False


class TestTheOffsetSettlesOnTheRoomTemperature:
    def test_the_comet_regulates_on_the_room_temperature(self):
        bt = _host()
        calibration_offset = _calibrate(bt)

        assert calibration_offset == pytest.approx(READING - ROOM)
        assert _regulated_reading(calibration_offset) == pytest.approx(ROOM)

    def test_the_offset_holds_over_later_cycles(self):
        """The written offset is read back without changing the reading.

        The reading stays at 22 °C whatever offset the device holds, so a
        calculation that adds the stored offset on top of it moves the
        offset again on every cycle.
        """
        bt = _host()
        trv = bt.real_trvs[ENTITY_ID]
        calibration_offsets = []
        for _ in range(4):
            calibration_offset = _calibrate(bt)
            trv.last_calibration = calibration_offset
            calibration_offsets.append(calibration_offset)

        assert calibration_offsets == pytest.approx([READING - ROOM] * 4)

    def test_a_device_offsetting_its_reading_settles_the_other_way(self):
        bt = _host(quirks=default)
        trv = bt.real_trvs[ENTITY_ID]
        calibration_offsets = []
        for _ in range(3):
            calibration_offset = _calibrate(bt)
            trv.last_calibration = calibration_offset
            trv.current_temperature = READING + calibration_offset
            calibration_offsets.append(calibration_offset)

        assert calibration_offsets == pytest.approx([ROOM - READING] * 3)


class TestTheAdjustmentsActInTheRoomDirection:
    def test_overheating_protection_lowers_the_effective_setpoint(self):
        """Above target + tolerance and idle, the protection closes the valve.

        The room is 0.5 K above 19 °C + 0.5 K, which the protection weighs
        eight times, so the regulated reading rises by 4 K from 20 °C to
        24 °C. For the COZB0001 that is a negative device offset, which
        lowers the setpoint it regulates on.
        """
        plain = _host(mode=CalibrationMode.AGGRESSIVE_CALIBRATION)
        protected = _host(
            mode=CalibrationMode.AGGRESSIVE_CALIBRATION, protect_overheating=True
        )
        for bt in (plain, protected):
            bt.heat_target_temperature = 19.0
            bt.hvac_action = HVACAction.IDLE

        assert _calibrate(plain) == pytest.approx(2.0)
        assert _calibrate(protected) == pytest.approx(-2.0)
        assert _regulated_reading(_calibrate(protected)) == pytest.approx(24.0)

    @pytest.mark.parametrize(
        ("action", "expected"), [(HVACAction.IDLE, 1.5), (HVACAction.HEATING, 2.0)]
    )
    def test_rounding_leans_towards_closing_when_idle(self, action, expected):
        """A 1.7 K correction on a 0.5 K grid.

        Idle rounds towards the higher regulated reading, 20.5 °C, which
        closes the valve; heating rounds towards 20.0 °C, which opens it.
        """
        bt = _host(step=0.5)
        bt.room_temperature = 20.3
        bt.hvac_action = action

        assert _calibrate(bt) == pytest.approx(expected)
