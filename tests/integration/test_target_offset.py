"""Exercise the target offset through Home Assistant."""

from homeassistant.const import ATTR_ENTITY_ID

from .conftest import (
    BT_ENTITY,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV


async def test_offset_number_changes_internal_target_only(hass):
    """The number changes regulation while the climate target stays unchanged."""
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    target = bt.heat_target_temperature

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.bt_test_target_offset", "value": 0.5},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert bt.target_offset == 0.5
    assert bt.heat_target_temperature == target
    assert hass.states.get(BT_ENTITY).attributes["temperature"] == target
    assert hass.states.get(BT_ENTITY).attributes["target_offset"] == 0.5
    assert (
        hass.states.get(BT_ENTITY).attributes["regulation_target_temp"] == target + 0.5
    )
    assert float(hass.states.get("number.bt_test_target_offset").state) == 0.5
