"""A head switched off for an open window takes no target from its knob.

Better Thermostat switches a head with an off mode off while a window is
open, and a head that is off reports no press: what it holds while off is
not the user's word on the room's target.
"""

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.core import Context

from .conftest import (
    BT_ENTITY,
    WINDOW_ID,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV


def _report(head) -> None:
    """Publish the head's state as a report of its own, as a press does."""
    head.async_set_context(Context())
    head.async_write_ha_state()


async def test_a_turn_at_a_head_switched_off_for_the_window_keeps_the_room_target(hass):
    """The knob turned at a head that is off for the window keeps the room target.

    The room's target is still its own once the window closes.
    """
    (head,) = await build_devices(hass, GENERIC_HEAT_TRV)
    set_room_sensor(hass, head.profile.current_temperature)
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(head.profile, with_window=True)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[head.entity_id]
    target = 20.5

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": target},
        blocking=True,
    )
    assert await wait_for(hass, lambda: head.target_temperature == target)

    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(
        hass,
        lambda: (
            head.hvac_mode == HVACMode.OFF
            and trv.system_mode_received
            and not bt.ignore_states
        ),
    )

    head._attr_target_temperature = 23.0
    _report(head)
    await hass.async_block_till_done()
    assert bt.heat_target_temperature == target
    assert bt.bt_hvac_mode == HVACMode.HEAT

    hass.states.async_set(WINDOW_ID, "off")
    assert await wait_for(hass, lambda: head.hvac_mode == HVACMode.HEAT, 3.0), (
        f"the closed window left the head in {head.hvac_mode}"
    )
    assert bt.heat_target_temperature == target
