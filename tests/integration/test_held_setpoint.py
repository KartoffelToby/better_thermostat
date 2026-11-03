"""A setpoint a head already holds when the room asks for it is the room's own."""

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.core import Context

from .conftest import (
    SENSOR_ID,
    TRV_ID,
    WINDOW_ID,
    make_entry,
    setup_entry,
    wait_for,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"


def _report(head) -> None:
    """Publish the head's state as a report of its own."""
    head.async_set_context(Context())
    head.async_write_ha_state()


async def test_a_turn_the_room_wants_anyway_is_not_read_as_a_press_later(
    hass, fake_trv
):
    """The head's next report leaves the room at the target the user set.

    The room sensor reads two degrees above the head, so the head carries the
    target two degrees down. With the window open the user turns the head to
    exactly the setpoint the room's new target will ask of it, which is not
    adopted. Once the window is shut the room asks the head for the value it
    already holds and writes nothing. The head's next report, which only
    moves its reading, carries that value again; it is not a press.
    """
    hass.states.async_set(SENSOR_ID, "21.5", {"unit_of_measurement": "°C"})
    hass.states.async_set(WINDOW_ID, "off")
    entry = make_entry(with_window=True)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[TRV_ID]

    hass.states.async_set(WINDOW_ID, "on")
    assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.OFF)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": 22.0},
        blocking=True,
    )
    fake_trv._attr_target_temperature = 20.0
    _report(fake_trv)
    await hass.async_block_till_done()
    assert bt.bt_target_temp == 22.0

    hass.states.async_set(WINDOW_ID, "off")
    assert await wait_for(
        hass,
        lambda: (
            fake_trv.hvac_mode == HVACMode.HEAT
            and trv.system_mode_received
            and not bt.ignore_states
        ),
    )
    assert fake_trv.target_temperature == 20.0

    fake_trv._attr_current_temperature = 19.4
    _report(fake_trv)
    await hass.async_block_till_done()
    assert await wait_for(hass, lambda: bt.bt_target_temp != 22.0, 1.0) is False, (
        f"the room took {bt.bt_target_temp}"
    )
