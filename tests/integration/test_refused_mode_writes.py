"""A mode write the TRV refuses, end to end through Home Assistant.

The TRV below refuses or drops mode commands the way a Zigbee device that
misses its acknowledgement does. What the user chose for the room has to
survive that, and survive the device's plain reports that follow it.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.core import Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.const import CalibrationMode

from .conftest import (
    BT_ENTITY,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV

# A calibration mode that registers no five-minute control tick, so the
# reconcile tick is the only periodic path that writes to the device.
RECONCILED_ONLY_TRV = replace(
    GENERIC_HEAT_TRV,
    name=f"{GENERIC_HEAT_TRV.name}_no_control_tick",
    calibration_mode=CalibrationMode.HEATING_POWER_CALIBRATION.value,
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


async def test_the_users_heat_survives_a_dropped_message_after_an_outage(
    hass, fake_trv
):
    """A mode channel marked by an earlier outage still gets HEAT through."""
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
        await wait_for(hass, lambda: not bt.ignore_states, timeout_s=5)
        await _settle(hass)
        set_room_sensor(hass, 18.2)
        assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.HEAT)
        await _settle(hass)

        _plain_report(fake_trv, 18.3)
        await _settle(hass)

    assert fake_trv.hvac_mode == HVACMode.HEAT
    assert bt.bt_hvac_mode == HVACMode.HEAT


@pytest.mark.parametrize(
    "fake_trv", [RECONCILED_ONLY_TRV], indirect=True, ids=profile_id
)
@pytest.mark.parametrize(
    ("held", "wanted"),
    [
        pytest.param(HVACMode.HEAT, HVACMode.OFF, id="refused_off"),
        pytest.param(HVACMode.OFF, HVACMode.HEAT, id="refused_heat"),
    ],
)
async def test_a_refused_mode_is_written_again_without_another_event(
    hass, fake_trv, held, wanted
):
    """The reconcile tick writes a refused mode again in a quiet room.

    The cycle that met the refusal ends without a retry of its own. No
    sensor reading or user action follows, so the five-minute reconcile
    tick is what finds the device in its old mode and queues the cycle
    that writes the mode again.
    """
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()
    if held == HVACMode.OFF:
        await _set_room_mode(hass, HVACMode.OFF)
        await wait_for(
            hass, lambda: not bt.ignore_states and fake_trv.hvac_mode == HVACMode.OFF
        )
        await _settle(hass)
    assert fake_trv.hvac_mode == held
    original = type(fake_trv).async_set_hvac_mode
    device = {"refusing": True}

    async def refuse(trv, hvac_mode):
        if trv is fake_trv and hvac_mode == wanted and device["refusing"]:
            trv.set_hvac_mode_calls.append(str(hvac_mode))
            raise HomeAssistantError("Failed to send request: timeout")
        return await original(trv, hvac_mode)

    with patch.object(type(fake_trv), "async_set_hvac_mode", refuse):
        await _set_room_mode(hass, wanted)
        assert await wait_for(hass, lambda: str(wanted) in fake_trv.set_hvac_mode_calls)
        await wait_for(hass, lambda: not bt.ignore_states, timeout_s=5)
        await _settle(hass)
        assert fake_trv.hvac_mode == held
        device["refusing"] = False

        for minutes in (6, 12):
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
            if await wait_for(hass, lambda: fake_trv.hvac_mode == wanted):
                break
        await _settle(hass)

    assert fake_trv.hvac_mode == wanted
    assert bt.bt_hvac_mode == wanted
