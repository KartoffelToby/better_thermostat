"""A mode write the TRV refuses, end to end through Home Assistant.

The TRV below refuses or drops mode commands the way a Zigbee device that
misses its acknowledgement does. What the user chose for the room has to
survive that, and survive the device's plain reports that follow it.
"""

import asyncio
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError

from .conftest import (
    BT_ENTITY,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)


async def _settle(hass, rounds=50):
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        await hass.async_block_till_done()


def _plain_report(fake_trv, temperature):
    """The TRV publishes a new temperature reading and nothing else."""
    fake_trv._attr_current_temperature = temperature
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()


async def _set_room_mode(hass, mode):
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_hvac_mode",
        {"entity_id": BT_ENTITY, "hvac_mode": mode},
        blocking=True,
    )


async def test_a_refused_off_is_retried_and_the_room_stays_off(hass, fake_trv):
    """The device's next plain report is no press back to heat."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()
    original = type(fake_trv).async_set_hvac_mode

    async def refuse_off(device, hvac_mode):
        if device is fake_trv and hvac_mode == HVACMode.OFF:
            device.set_hvac_mode_calls.append(str(hvac_mode))
            raise HomeAssistantError("Failed to send request: timeout")
        return await original(device, hvac_mode)

    with patch.object(type(fake_trv), "async_set_hvac_mode", refuse_off):
        await _set_room_mode(hass, HVACMode.OFF)
        assert await wait_for(hass, lambda: "off" in fake_trv.set_hvac_mode_calls)
        await wait_for(hass, lambda: not bt.ignore_states, timeout_s=5)
        refused = fake_trv.set_hvac_mode_calls.count("off")

        _plain_report(fake_trv, 18.4)
        await _settle(hass)
        assert bt.bt_hvac_mode == HVACMode.OFF

        set_room_sensor(hass, 18.2)
        assert await wait_for(
            hass, lambda: fake_trv.set_hvac_mode_calls.count("off") > refused
        )
        await _settle(hass)

    assert bt.bt_hvac_mode == HVACMode.OFF


async def test_a_mode_that_landed_despite_its_error_leaves_heat_its_retry(
    hass, fake_trv
):
    """The user's later HEAT survives one dropped message."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()
    original = type(fake_trv).async_set_hvac_mode
    device = {"applies_and_raises": True, "drops": 0}

    async def flaky(trv, hvac_mode):
        if trv is fake_trv and device["applies_and_raises"]:
            await original(trv, hvac_mode)
            raise HomeAssistantError("Failed to send request: timeout (applied)")
        if trv is fake_trv and device["drops"] > 0:
            device["drops"] -= 1
            trv.set_hvac_mode_calls.append(f"dropped {hvac_mode}")
            raise HomeAssistantError("Failed to send request: timeout")
        return await original(trv, hvac_mode)

    with patch.object(type(fake_trv), "async_set_hvac_mode", flaky):
        await _set_room_mode(hass, HVACMode.OFF)
        await wait_for(
            hass, lambda: not bt.ignore_states and fake_trv.hvac_mode == HVACMode.OFF
        )
        await _settle(hass)
        device["applies_and_raises"] = False

        device["drops"] = 1
        await _set_room_mode(hass, HVACMode.HEAT)
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.HEAT)
        await _settle(hass)

        _plain_report(fake_trv, 18.3)
        await _settle(hass)

    assert fake_trv.hvac_mode == HVACMode.HEAT
    assert bt.bt_hvac_mode == HVACMode.HEAT
