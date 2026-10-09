"""The AVATTO ME167 applies its local calibration with the opposite sign.

The device subtracts the offset it holds from the temperature it measures,
reports that difference, and reports the offset as it was written. A device
that offsets its reading the common way adds the offset and reports the sum.
The local calibration has to land either device on the room temperature, and
has to stay there from one control cycle to the next.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType

from homeassistant.components.climate.const import HVACAction
import pytest

from custom_components.better_thermostat.calibration import calculate_calibration_local
from custom_components.better_thermostat.model_fixes import ME167, default
from custom_components.better_thermostat.model_fixes.model_quirks import (
    local_calibration_reverses_sign,
    local_calibration_shifts_setpoint,
)
from custom_components.better_thermostat.utils.const import (
    CONF_PROTECT_OVERHEATING,
    CalibrationMode,
)
from tests.factories import ThermostatStandIn, make_state, trv_from_legacy_dict

ENTITY_ID = "climate.me167"
ROOM = 22.0
MEASURED = 20.0
CYCLES = 6


@dataclass
class SimulatedTrv:
    """A TRV that applies the offset it holds to what it measures.

    ``sign`` is +1 for a device that adds the offset to its reading and -1
    for one that subtracts it. Either one reports the offset as written.
    """

    sign: float
    measured: float = MEASURED
    held_offset: float = 0.0

    @property
    def reported(self) -> float:
        return self.measured + self.sign * self.held_offset


def _host(
    device: SimulatedTrv,
    *,
    quirks: ModuleType | None = ME167,
    step: float = 0.1,
    protect_overheating: bool = False,
    mode: CalibrationMode = CalibrationMode.DEFAULT,
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
                "current_temperature": device.reported,
                "last_calibration": device.held_offset,
                "local_calibration_step": step,
                "min_local_calibration": -7.0,
                "max_local_calibration": 7.0,
                "target_temp_step": 0.5,
                "min_temp": 5.0,
                "max_temp": 35.0,
                "model_quirks": quirks,
            },
        )
    }
    return bt


def _calibrate(bt: ThermostatStandIn) -> float:
    calibration_offset = calculate_calibration_local(bt, ENTITY_ID)
    assert calibration_offset is not None
    return calibration_offset


def _run_cycles(bt: ThermostatStandIn, device: SimulatedTrv) -> list[float]:
    """Write each offset to the device and read the device back, cycle by cycle."""
    trv = bt.real_trvs[ENTITY_ID]
    reported = []
    for _ in range(CYCLES):
        device.held_offset = _calibrate(bt)
        trv.last_calibration = device.held_offset
        trv.current_temperature = device.reported
        reported.append(device.reported)
    return reported


class TestTheMe167IsDeclaredReversed:
    def test_the_me167_answers_that_it_reverses_the_sign(self):
        bt = _host(SimulatedTrv(sign=-1.0))
        assert local_calibration_reverses_sign(bt, ENTITY_ID) is True

    def test_the_me167_offsets_its_reading_not_its_setpoint(self):
        bt = _host(SimulatedTrv(sign=-1.0))
        assert local_calibration_shifts_setpoint(bt, ENTITY_ID) is False

    def test_a_module_without_the_answer_keeps_the_sign(self):
        bt = _host(SimulatedTrv(sign=1.0), quirks=default)
        assert local_calibration_reverses_sign(bt, ENTITY_ID) is False

    def test_a_trv_without_a_loaded_module_keeps_the_sign(self):
        bt = _host(SimulatedTrv(sign=1.0), quirks=None)
        assert local_calibration_reverses_sign(bt, ENTITY_ID) is False


class TestTheReadingSettlesOnTheRoomTemperature:
    def test_the_me167_reports_the_room_temperature_on_every_cycle(self):
        device = SimulatedTrv(sign=-1.0)
        bt = _host(device)

        assert _run_cycles(bt, device) == pytest.approx([ROOM] * CYCLES)

    def test_the_me167_holds_the_offset_in_its_own_sign(self):
        """Lifting the reading by 2 K takes an offset of -2 on this device."""
        device = SimulatedTrv(sign=-1.0)
        bt = _host(device)
        _run_cycles(bt, device)

        assert device.held_offset == pytest.approx(MEASURED - ROOM)

    def test_the_me167_settles_from_an_offset_it_already_holds(self):
        """A restart finds the offset of an earlier run on the device."""
        device = SimulatedTrv(sign=-1.0, held_offset=3.0)
        bt = _host(device)

        assert _run_cycles(bt, device) == pytest.approx([ROOM] * CYCLES)

    def test_a_device_adding_the_offset_settles_with_the_default_module(self):
        device = SimulatedTrv(sign=1.0)
        bt = _host(device, quirks=default)

        assert _run_cycles(bt, device) == pytest.approx([ROOM] * CYCLES)
        assert device.held_offset == pytest.approx(ROOM - MEASURED)


class TestTheAdjustmentsActInTheRoomDirection:
    def test_overheating_protection_raises_the_reading(self):
        """Above target + tolerance and idle, the protection closes the valve.

        The room is 0.5 K above 21 °C + 0.5 K, which the protection weighs
        eight times, so the reported reading rises by 4 K from 22 °C to
        26 °C. For the ME167 that is a negative device offset.
        """
        plain_device = SimulatedTrv(sign=-1.0)
        protected_device = SimulatedTrv(sign=-1.0)
        plain = _host(plain_device, mode=CalibrationMode.AGGRESSIVE_CALIBRATION)
        protected = _host(
            protected_device,
            mode=CalibrationMode.AGGRESSIVE_CALIBRATION,
            protect_overheating=True,
        )
        for bt in (plain, protected):
            bt.heat_target_temperature = 21.0
            bt.hvac_action = HVACAction.IDLE

        plain_device.held_offset = _calibrate(plain)
        protected_device.held_offset = _calibrate(protected)

        assert plain_device.reported == pytest.approx(ROOM)
        assert protected_device.reported == pytest.approx(26.0)

    @pytest.mark.parametrize(
        ("action", "reported"), [(HVACAction.IDLE, 22.5), (HVACAction.HEATING, 22.0)]
    )
    def test_rounding_leans_towards_closing_when_idle(self, action, reported):
        """A 2.3 K correction on a 0.5 K grid.

        Idle rounds towards the higher reported reading, 22.5 °C, which
        closes the valve; heating rounds towards 22.0 °C, which opens it.
        """
        device = SimulatedTrv(sign=-1.0)
        bt = _host(device, step=0.5)
        bt.room_temperature = 22.3
        bt.hvac_action = action

        device.held_offset = _calibrate(bt)

        assert device.reported == pytest.approx(reported)
