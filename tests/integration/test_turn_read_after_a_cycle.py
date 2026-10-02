"""A turn read at the end of a cycle is answered like one read outside it."""

import asyncio

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.core import Context
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import setup_test_component_platform

from .conftest import (
    SENSOR_ID,
    TRV_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"
SECOND_TRV_ID = "climate.second_trv"


class SecondTrv(FakeTrvEntity):
    """A second head of the same kind in the same room."""

    _attr_name = "second trv"


def _report(head) -> None:
    """Publish the head's state as a report of its own."""
    head.async_set_context(Context())
    head.async_write_ha_state()


async def test_a_head_turned_to_the_rooms_target_during_a_cycle_is_corrected(hass):
    """A head turned to the room's own target carries its share of it again.

    The room sensor reads warmer than the heads, so each head carries the
    target corrected down. While a cycle writes the room's new target, the
    first head confirms its write and the user turns it to exactly that
    target before the write to the second head is through. The room adopts
    the turn and its target stays what it was, and the first head goes back
    to its corrected setpoint.
    """
    first, second = FakeTrvEntity(), SecondTrv()
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [first, second])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    hass.states.async_set(SENSOR_ID, "22.5", {"unit_of_measurement": "°C"})
    entry = make_entry()
    data = dict(entry.data)
    data["thermostat"] = [
        data["thermostat"][0],
        {**data["thermostat"][0], "trv": SECOND_TRV_ID},
    ]
    entry = type(entry)(
        domain=entry.domain, version=entry.version, data=data, title=entry.title
    )
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[TRV_ID]

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": 20.0},
        blocking=True,
    )
    assert await wait_for(
        hass,
        lambda: first.target_temperature == 17.0 and second.target_temperature == 17.0,
    )
    # With no cycle running or queued, the cycle the new target starts is the
    # last one unless the turn read at its end asks for another.
    assert await wait_for(
        hass, lambda: bt.control_queue_task.empty() and not bt.ignore_states
    )

    reached, release = asyncio.Event(), asyncio.Event()
    apply = second.async_set_temperature

    async def held(**kwargs):
        reached.set()
        await release.wait()
        await apply(**kwargs)

    second.async_set_temperature = held
    command = hass.async_create_task(
        hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_TEMPERATURE,
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
    )
    await asyncio.wait_for(reached.wait(), 5.0)
    assert await wait_for(
        hass, lambda: first.target_temperature == 20.0 and trv.target_temp_received
    )
    first._attr_target_temperature = 23.0
    _report(first)
    second.async_set_temperature = apply
    release.set()
    await command

    assert await wait_for(hass, lambda: first.target_temperature == 20.0, 3.0), (
        f"the head stayed at {first.target_temperature}"
    )
    assert bt.bt_target_temp == 23.0
