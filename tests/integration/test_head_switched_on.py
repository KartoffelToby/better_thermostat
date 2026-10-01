"""A head switched on at the device turns the room on at the room's target."""

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.core import Context

from .conftest import SENSOR_ID, make_entry, setup_entry, wait_for, wait_for_startup

BT_ENTITY = "climate.bt_test"


def _report(fake_trv) -> None:
    """Publish the device's state as a report of its own, as a press does."""
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()


async def test_a_head_switched_on_does_not_bring_what_was_turned_while_it_was_off(
    hass, fake_trv
):
    """The knob turned while the head was off is written over.

    The user switches the room off, turns the device's knob while it is off,
    and switches the device on. The turn was not the user's word while the
    device was off, and the report that switches it on does not make it one.
    """
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[fake_trv.entity_id]
    target, turned = 20.5, 22.5

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": target},
        blocking=True,
    )
    assert await wait_for(
        hass,
        lambda: (
            fake_trv.target_temperature == target
            and trv.target_temp_received
            and not bt.ignore_states
        ),
    )
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": BT_ENTITY, "hvac_mode": HVACMode.OFF},
        blocking=True,
    )
    assert await wait_for(
        hass,
        lambda: (
            fake_trv.hvac_mode == HVACMode.OFF
            and trv.system_mode_received
            and not bt.ignore_states
        ),
    )

    fake_trv._attr_target_temperature = turned
    _report(fake_trv)
    await hass.async_block_till_done()
    assert bt.bt_target_temp == target

    fake_trv._attr_hvac_mode = HVACMode.HEAT
    _report(fake_trv)
    assert await wait_for(hass, lambda: bt.hvac_mode == HVACMode.HEAT, 3.0)
    assert await wait_for(hass, lambda: bt.bt_target_temp == target, 3.0), (
        f"the room took {bt.bt_target_temp}"
    )
    assert await wait_for(hass, lambda: fake_trv.target_temperature == target, 3.0), (
        f"the device stayed at {fake_trv.target_temperature}"
    )
