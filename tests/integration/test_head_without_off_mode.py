"""A head without an off mode carries the room's mode on its setpoint.

The user switches such a room off by turning the head to its minimum and on
again by turning it up. Better Thermostat parks the head at the same minimum
itself while the room calls for no heat, and that value coming back from the
head is Better Thermostat's own write, not the user switching the room off.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Context
import pytest

from .conftest import (
    OUTDOOR_ID,
    WINDOW_ID,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

HEAD_WITHOUT_OFF = replace(
    GENERIC_HEAT_TRV, name="head_without_off", hvac_modes=(HVACMode.HEAT,)
)


@pytest.fixture(autouse=True)
def _no_write_budget():
    """Let a write follow the previous one without waiting out the budget."""
    with patch(WRITE_BUDGET, 0.0):
        yield


def _set_outdoor(hass, celsius: float) -> None:
    hass.states.async_set(
        OUTDOOR_ID, str(celsius), {"unit_of_measurement": UnitOfTemperature.CELSIUS}
    )


async def _room(
    hass, *, outdoor_celsius: float, room_celsius: float, with_window: bool = False
):
    """Set up a room heated by one head without an off mode, and settle it."""
    (device,) = await build_devices(hass, HEAD_WITHOUT_OFF)
    set_room_sensor(hass, room_celsius)
    _set_outdoor(hass, outdoor_celsius)
    if with_window:
        hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(
        HEAD_WITHOUT_OFF,
        with_outdoor_sensor=True,
        off_temperature=15,
        with_window=with_window,
    )
    entry.data["thermostat"][0]["advanced"]["no_off_system_mode"] = True
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return bt, device


def _settled(bt) -> bool:
    """Whether Better Thermostat has every write to the head confirmed."""
    return not bt.ignore_states and all(
        trv.system_mode_received and trv.target_temperature_received
        for trv in bt.real_trvs.values()
    )


def _publish(device, **attributes) -> None:
    """Let the head publish its state, with ``attributes`` changed at the head."""
    for name, value in attributes.items():
        setattr(device, f"_attr_{name}", value)
    device.async_set_context(Context())
    device.async_write_ha_state()


async def test_a_head_parked_for_warm_weather_does_not_switch_the_room_off(hass):
    """The minimum written for warm weather is no off, and the room heats once it is cold."""
    bt, device = await _room(hass, outdoor_celsius=25.0, room_celsius=19.0)
    assert await wait_for(
        hass,
        lambda: bt.call_for_heat is False and device.target_temperature == 5.0,
        timeout_seconds=3.0,
    ), (
        f"warm weather should park the head at its minimum, it holds "
        f"{device.target_temperature} with call_for_heat={bt.call_for_heat}"
    )

    # The head reports on its own, carrying the minimum it was parked at.
    _publish(device, current_temperature=19.4)
    await hass.async_block_till_done()
    await wait_for(hass, lambda: bt.bt_hvac_mode == HVACMode.OFF, timeout_seconds=1.0)
    assert bt.bt_hvac_mode == HVACMode.HEAT, (
        "the head reporting the minimum Better Thermostat parked it at "
        f"switched the room to {bt.bt_hvac_mode}"
    )

    # Three days of frost bring the damped outdoor temperature down to it.
    system_utcnow = bt.clock.utcnow
    with patch.object(bt.clock, "utcnow", lambda: system_utcnow() + timedelta(days=3)):
        _set_outdoor(hass, 0.0)
        set_room_sensor(hass, 17.0)
        assert await wait_for(
            hass,
            lambda: (
                bt.call_for_heat is True and (device.target_temperature or 0.0) > 5.0
            ),
            timeout_seconds=3.0,
        ), (
            f"the cold room is not heated: room mode {bt.bt_hvac_mode}, "
            f"call_for_heat={bt.call_for_heat}, head at {device.target_temperature}"
        )


async def test_turning_the_head_to_its_minimum_and_back_switches_the_room(hass):
    """A turn at the head still switches the room off and on again."""
    bt, device = await _room(hass, outdoor_celsius=0.0, room_celsius=19.0)
    assert await wait_for(
        hass,
        lambda: (
            not bt.ignore_states
            and (device.target_temperature or 0.0) > 5.0
            and all(
                trv.system_mode_received and trv.target_temperature_received
                for trv in bt.real_trvs.values()
            )
        ),
    )

    _publish(device, target_temperature=5.0)
    assert await wait_for(
        hass, lambda: bt.bt_hvac_mode == HVACMode.OFF, timeout_seconds=2.0
    ), f"turning the head to its minimum left the room in {bt.bt_hvac_mode}"
    assert await wait_for(
        hass,
        lambda: (
            not bt.ignore_states
            and all(trv.target_temperature_received for trv in bt.real_trvs.values())
        ),
    )

    _publish(device, target_temperature=21.0)
    assert await wait_for(
        hass, lambda: bt.bt_hvac_mode == HVACMode.HEAT, timeout_seconds=2.0
    ), f"turning the head up left the room in {bt.bt_hvac_mode}"


async def _off_room_with_the_window_open(hass):
    """Switch the room off at the head, open the window, and settle it."""
    bt, device = await _room(
        hass, outdoor_celsius=0.0, room_celsius=19.0, with_window=True
    )
    assert await wait_for(
        hass, lambda: _settled(bt) and (device.target_temperature or 0.0) > 5.0
    )
    _publish(device, target_temperature=5.0)
    assert await wait_for(hass, lambda: bt.bt_hvac_mode == HVACMode.OFF)
    assert await wait_for(hass, lambda: _settled(bt))

    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(hass, lambda: bt.window_open is True)
    # The off room writes nothing for the open window; the turn comes once
    # the cycle the window requested has run.
    async with asyncio.timeout(10):
        await bt.window_queue_task.join()
        await bt.control_queue_task.join()
    assert _settled(bt)
    return bt, device


async def _heating_room_with_the_window_open(hass):
    """Open the window of a heating room, and let the head be parked."""
    bt, device = await _room(
        hass, outdoor_celsius=0.0, room_celsius=19.0, with_window=True
    )
    assert await wait_for(
        hass, lambda: _settled(bt) and (device.target_temperature or 0.0) > 5.0
    )
    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(
        hass, lambda: device.target_temperature == 5.0 and _settled(bt)
    )
    return bt, device


async def test_a_turn_up_with_the_window_open_heats_once_the_window_closes(hass):
    """The head turned up in an off room with the window open switches the room on.

    The turn is the user's target, but the open window still holds the head
    at its minimum: it is turned back at once, not left heating into the
    window until the next routine cycle. Once the window closes, the room
    heats to the turned target.
    """
    bt, device = await _off_room_with_the_window_open(hass)
    writes_before_turn = len(device.set_temperature_calls)

    _publish(device, target_temperature=21.0)
    assert await wait_for(
        hass,
        lambda: device.set_temperature_calls[writes_before_turn:] == [5.0],
        timeout_seconds=2.0,
    ), (
        "the head turned up with the window open was not turned back, "
        f"it holds {device.target_temperature} after the writes "
        f"{device.set_temperature_calls[writes_before_turn:]}"
    )
    assert device.target_temperature == 5.0
    assert bt.bt_hvac_mode == HVACMode.HEAT, (
        f"the turn with the window open left the room in {bt.bt_hvac_mode}"
    )
    assert bt.heat_target_temperature == 21.0

    hass.states.async_set(WINDOW_ID, "off")
    assert await wait_for(
        hass,
        lambda: bt.window_open is False and (device.target_temperature or 0.0) > 5.0,
        timeout_seconds=3.0,
    ), (
        f"the closed window left the head at {device.target_temperature}, "
        f"room mode {bt.bt_hvac_mode}"
    )
    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert bt.heat_target_temperature == 21.0


async def test_a_turn_to_the_minimum_with_the_window_open_switches_the_room_off(hass):
    """The head turned up and back to its minimum with the window open stays off.

    Both reports arrive before Better Thermostat turns the head back, so the
    minimum is the user's and not the minimum written for the window. The
    room is off once the window closes.
    """
    bt, device = await _heating_room_with_the_window_open(hass)

    _publish(device, target_temperature=21.0)
    _publish(device, target_temperature=5.0)
    assert await wait_for(
        hass, lambda: bt.bt_hvac_mode == HVACMode.OFF, timeout_seconds=2.0
    ), f"the turn to the minimum left the room in {bt.bt_hvac_mode}"

    hass.states.async_set(WINDOW_ID, "off")
    assert await wait_for(hass, lambda: bt.window_open is False and _settled(bt))
    async with asyncio.timeout(10):
        await bt.window_queue_task.join()
        await bt.control_queue_task.join()
    assert bt.bt_hvac_mode == HVACMode.OFF, (
        f"the closed window put the room in {bt.bt_hvac_mode}"
    )
    assert device.target_temperature == 5.0


async def test_the_minimum_written_for_the_open_window_is_no_turn(hass):
    """The head reporting the minimum it was parked at keeps the room's target.

    Once the window closes, the room heats to the target it had before.
    """
    bt, device = await _heating_room_with_the_window_open(hass)
    target = bt.heat_target_temperature
    assert target is not None and target > 5.0

    _publish(device, current_temperature=18.4)
    await hass.async_block_till_done()
    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert bt.heat_target_temperature == target

    hass.states.async_set(WINDOW_ID, "off")
    assert await wait_for(
        hass,
        lambda: bt.window_open is False and (device.target_temperature or 0.0) > 5.0,
        timeout_seconds=3.0,
    ), f"the closed window left the head at {device.target_temperature}"
    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert bt.heat_target_temperature == target


async def _room_whose_window_just_closed(hass, *, watchdog_gives_up: bool):
    """Open and close the window of a heating room, losing the restore write.

    The head stays at the minimum Better Thermostat wrote for the open
    window. With ``watchdog_gives_up`` the wait for the lost write runs out
    before the head reports again.
    """
    bt, device = await _room(
        hass, outdoor_celsius=0.0, room_celsius=19.0, with_window=True
    )
    assert await wait_for(
        hass, lambda: _settled(bt) and (device.target_temperature or 0.0) > 5.0
    )
    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(
        hass, lambda: device.target_temperature == 5.0 and _settled(bt)
    )
    writes_before_close = len(device.set_temperature_calls)

    device.drop_next_setpoint_write = True
    hass.states.async_set(WINDOW_ID, "off")
    assert await wait_for(
        hass,
        lambda: (
            bt.window_open is False
            and len(device.set_temperature_calls) > writes_before_close
        ),
    )
    restore_write = device.set_temperature_calls[writes_before_close]
    assert isinstance(restore_write, float) and restore_write > 5.0
    assert device.target_temperature == 5.0
    if watchdog_gives_up:
        assert await wait_for(hass, lambda: _settled(bt), timeout_seconds=30.0)
    return bt, device


async def test_a_report_while_the_restore_write_is_lost_keeps_the_room_heating(hass):
    """The head still at the open window's minimum reports before the restore lands."""
    bt, device = await _room_whose_window_just_closed(hass, watchdog_gives_up=False)

    _publish(device, current_temperature=18.6)
    await hass.async_block_till_done()
    await wait_for(hass, lambda: bt.bt_hvac_mode == HVACMode.OFF, timeout_seconds=1.0)
    assert bt.bt_hvac_mode == HVACMode.HEAT, (
        "the head reporting the minimum written for the open window "
        f"switched the room to {bt.bt_hvac_mode}"
    )


async def test_a_report_after_the_restore_write_timed_out_keeps_the_room_heating(hass):
    """Once the wait for the lost restore gives up, the minimum is still no press.

    The head reports the minimum again, the room keeps heating, and Better
    Thermostat writes a heating setpoint to the head once more.
    """
    bt, device = await _room_whose_window_just_closed(hass, watchdog_gives_up=True)
    assert device.target_temperature == 5.0
    writes_before_report = len(device.set_temperature_calls)

    _publish(device, current_temperature=18.6)
    assert await wait_for(hass, lambda: (device.target_temperature or 0.0) > 5.0), (
        f"the head was left at {device.target_temperature} after the writes "
        f"{device.set_temperature_calls[writes_before_report:]}, "
        f"room mode {bt.bt_hvac_mode}"
    )
    assert bt.bt_hvac_mode == HVACMode.HEAT, (
        "the head reporting the minimum written for the open window "
        f"switched the room to {bt.bt_hvac_mode}"
    )
