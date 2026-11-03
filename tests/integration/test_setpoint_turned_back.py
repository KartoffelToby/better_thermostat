"""A setpoint turned at the device and back again ends where the user left it."""

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.core import Context

from .conftest import SENSOR_ID, make_entry, setup_entry, wait_for, wait_for_startup

BT_ENTITY = "climate.bt_test"


def _turn(fake_trv, value: float) -> None:
    """Turn the setpoint at the device, the way a knob press reaches HA."""
    fake_trv._attr_target_temperature = value
    fake_trv.async_set_context(Context())
    fake_trv.async_write_ha_state()


async def test_a_head_turned_back_to_its_last_confirmed_setpoint_is_adopted(
    hass, fake_trv
):
    """Only the turn back is the user's word, though BT once wrote that value.

    The user sets the room on the entity, the device confirms it, and the
    user turns the device up and then back to where it was.
    """
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[fake_trv.entity_id]

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": 20.5},
        blocking=True,
    )
    assert await wait_for(
        hass,
        lambda: (
            fake_trv.target_temperature == 20.5
            and trv.target_temp_received
            and not bt.ignore_states
        ),
    )

    _turn(fake_trv, 22.0)
    assert await wait_for(hass, lambda: bt.bt_target_temp == 22.0, 3.0)

    _turn(fake_trv, 20.5)
    assert await wait_for(hass, lambda: bt.bt_target_temp == 20.5, 3.0), (
        f"the room stayed at {bt.bt_target_temp}"
    )
    assert await wait_for(hass, lambda: fake_trv.target_temperature == 20.5, 3.0)
