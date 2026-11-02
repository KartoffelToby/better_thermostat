"""Switching the thermostat off closes the valve of a valve-driven device.

A device driven by its valve keeps the last percentage it was given; the
device's own off mode does not move it. Each controller that can drive the
valve stops publishing a valve command while the thermostat is off, so the
close has to come from the switch-off itself, for every controller alike.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
import pytest

from custom_components.better_thermostat.utils.const import CalibrationMode
from custom_components.better_thermostat.utils.scheduler import request_control_cycle

from .conftest import (
    BT_ENTITY,
    WINDOW_ID,
    WRITE_BUDGET,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import TRV_ID, VALVE_TRV

_VALVE_CONTROLLERS = [
    CalibrationMode.MPC_CALIBRATION,
    CalibrationMode.MPC_V2_CALIBRATION,
    CalibrationMode.TPI_CALIBRATION,
    CalibrationMode.PID_CALIBRATION,
    CalibrationMode.HEATING_POWER_CALIBRATION,
]


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(VALVE_TRV, name=f"valve_trv_{mode.value}", calibration_mode=mode.value)
        for mode in _VALVE_CONTROLLERS
    ],
    indirect=True,
    ids=profile_id,
)
async def test_switching_off_closes_the_valve_once_and_on_reopens_it(hass, fake_trv):
    """Off writes 0 % to the valve exactly once; on lets the controller open it.

    The room calls for heat throughout, so the controller would keep the
    valve open on its own: only the switch-off can close it, and only the
    controller can open it again afterwards.
    """
    set_room_sensor(hass, 17.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    valve_number = fake_trv.valve_number
    assert valve_number is not None

    def _valve_open() -> bool:
        calls = valve_number.set_value_calls
        return bool(calls) and calls[-1] > 0

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 24.0},
            blocking=True,
        )
        assert await wait_for(hass, _valve_open), valve_number.set_value_calls

        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": BT_ENTITY, "hvac_mode": HVACMode.OFF},
            blocking=True,
        )
        assert await wait_for(
            hass, lambda: valve_number.set_value_calls[-1] == pytest.approx(0.0)
        ), valve_number.set_value_calls
        writes_at_close = len(valve_number.set_value_calls)

        # Further cycles while off find the valve already closed.
        for _ in range(3):
            request_control_cycle(bt)
            await bt.control_queue_task.join()
            await hass.async_block_till_done()
        assert len(valve_number.set_value_calls) == writes_at_close
        assert float(hass.states.get(valve_number.entity_id).state) == pytest.approx(
            0.0
        )

        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": BT_ENTITY, "hvac_mode": HVACMode.HEAT},
            blocking=True,
        )
        assert await wait_for(hass, _valve_open), valve_number.set_value_calls

    assert bt.real_trvs[TRV_ID].last_valve_percent == pytest.approx(
        valve_number.set_value_calls[-1]
    )


@pytest.mark.parametrize(
    "fake_trv",
    [
        replace(VALVE_TRV, name=f"valve_trv_{mode.value}", calibration_mode=mode.value)
        for mode in _VALVE_CONTROLLERS
    ],
    indirect=True,
    ids=profile_id,
)
async def test_an_open_window_closes_the_valve(hass, fake_trv):
    """An open window switches the device off, and its valve closes with it.

    The window leaves the thermostat's own mode on heat; the valve closes
    because the device is held off, not because a controller says so.
    """
    set_room_sensor(hass, 17.0)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(fake_trv.profile, with_window=True)
    await setup_entry(hass, entry)
    await wait_for_startup(hass, entry)
    valve_number = fake_trv.valve_number
    assert valve_number is not None

    def _valve_open() -> bool:
        calls = valve_number.set_value_calls
        return bool(calls) and calls[-1] > 0

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 24.0},
            blocking=True,
        )
        assert await wait_for(hass, _valve_open), valve_number.set_value_calls

        hass.states.async_set(WINDOW_ID, "on")
        assert await wait_for(
            hass, lambda: valve_number.set_value_calls[-1] == pytest.approx(0.0)
        ), valve_number.set_value_calls
