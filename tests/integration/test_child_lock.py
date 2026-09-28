"""A child-locked device is held on what Better Thermostat commands.

A press at a locked device does not speak for the user, so Better Thermostat
does not adopt it, and it puts the device back on its own command as soon as
the device reports the press rather than on some later cycle.
"""

from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.core import Context
import pytest

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV


@pytest.fixture(autouse=True)
def _no_write_budget():
    """Let a write follow the previous one without waiting out the budget."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _locked_room(hass):
    """Set up a room whose only device is child-locked, and settle it."""
    (device,) = await build_devices(hass, GENERIC_HEAT_TRV)
    set_room_sensor(hass, 19.0)
    entry = make_entry(GENERIC_HEAT_TRV)
    entry.data["thermostat"][0]["advanced"]["child_lock"] = True
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": 21.0},
        blocking=True,
    )
    assert await wait_for(
        hass,
        lambda: (
            not bt.ignore_states
            and device.hvac_mode == HVACMode.HEAT
            # The room asks for 21 °C against 19 °C, so the device is driven
            # well above its minimum once the command has gone out.
            and (device.target_temperature or 0.0) >= 21.0
            and all(
                trv.system_mode_received and trv.target_temp_received
                for trv in bt.real_trvs.values()
            )
        ),
    )
    return bt, device


def _press(device, *, hvac_mode=None, temperature=None) -> None:
    """Change the device at the device itself and let it publish the change."""
    if hvac_mode is not None:
        device._attr_hvac_mode = hvac_mode
    if temperature is not None:
        device._attr_target_temperature = temperature
    device.async_set_context(Context())
    device.async_write_ha_state()


@pytest.mark.parametrize("turn", [3.0, -3.0])
async def test_a_locked_knob_turn_is_turned_back_at_once(hass, turn):
    """A setpoint turned at a locked device returns to the commanded one."""
    bt, device = await _locked_room(hass)
    commanded = device.target_temperature

    _press(device, temperature=commanded + turn)

    assert await wait_for(
        hass, lambda: device.target_temperature == commanded, timeout_s=2.0
    )
    assert bt.bt_target_temp == 21.0


async def test_a_locked_mode_press_is_turned_back_at_once(hass):
    """A mode switched at a locked device returns to the commanded one."""
    bt, device = await _locked_room(hass)

    _press(device, hvac_mode=HVACMode.OFF)

    assert await wait_for(
        hass, lambda: device.hvac_mode == HVACMode.HEAT, timeout_s=2.0
    )
    assert bt.bt_hvac_mode == HVACMode.HEAT
