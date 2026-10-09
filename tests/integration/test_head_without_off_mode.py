"""A head without an off mode carries the room's mode on its setpoint.

The user switches such a room off by turning the head to its minimum and on
again by turning it up. Better Thermostat parks the head at the same minimum
itself while the room calls for no heat, and that value coming back from the
head is Better Thermostat's own write, not the user switching the room off.
"""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import Context
import pytest

from .conftest import (
    OUTDOOR_ID,
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


async def _room(hass, *, outdoor_celsius: float, room_celsius: float):
    """Set up a room heated by one head without an off mode, and settle it."""
    (device,) = await build_devices(hass, HEAD_WITHOUT_OFF)
    set_room_sensor(hass, room_celsius)
    _set_outdoor(hass, outdoor_celsius)
    entry = make_entry(HEAD_WITHOUT_OFF, with_outdoor_sensor=True, off_temperature=15)
    entry.data["thermostat"][0]["advanced"]["no_off_system_mode"] = True
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    return bt, device


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
