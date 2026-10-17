"""A turn read at the end of a cycle is answered like one read outside it."""

from unittest.mock import patch

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.core import Context

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
from .device_profiles import GROUP_OF_THREE
from .write_hold import holding_next_write, poll_until


async def _set_on_entity(hass, value: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": value},
        blocking=True,
    )


async def test_a_head_turned_to_the_rooms_target_during_a_cycle_is_corrected(hass):
    """A head turned to the room's own target carries its share of it again.

    The room sensor reads three degrees warmer than the heads, so each head
    carries the target corrected down by three. While a cycle writes the
    room's new target, the first head confirms its write and the user turns
    it to exactly that target before the write to the last head is through.
    The room adopts the turn and its target stays what it was, and the first
    head goes back to its corrected setpoint instead of heating to the
    uncorrected one.
    """
    heads = await build_devices(hass, *GROUP_OF_THREE.profiles)
    set_room_sensor(hass, 22.5)
    entry = make_entry(GROUP_OF_THREE)
    first, last = heads[0], heads[-1]

    with patch(WRITE_BUDGET, 0.0):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        trv = bt.real_trvs[first.entity_id]

        await _set_on_entity(hass, 20.0)
        assert await wait_for(
            hass, lambda: all(head.target_temperature == 17.0 for head in heads)
        )

        async with holding_next_write(last, "async_set_temperature") as hold:
            await _set_on_entity(hass, 23.0)
            await hold.wait_reached(hass)
            assert await poll_until(
                hass,
                lambda: (
                    first.target_temperature == 20.0 and trv.target_temperature_received
                ),
            )
            first._attr_target_temperature = 23.0
            first.async_set_context(Context())
            first.async_write_ha_state()
            hold.release()

        assert await wait_for(hass, lambda: first.target_temperature == 20.0, 3.0), (
            f"the head stayed at {first.target_temperature}"
        )
        assert bt.heat_target_temperature == 23.0
