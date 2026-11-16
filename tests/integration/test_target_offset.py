"""Exercise the target offset through Home Assistant."""

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.util.unit_system import METRIC_SYSTEM, US_CUSTOMARY_SYSTEM
import pytest

from .conftest import (
    BT_ENTITY,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV


@pytest.mark.parametrize(
    ("unit_system", "number_value", "kelvin_offset"),
    [
        pytest.param(METRIC_SYSTEM, 0.5, 0.5, id="celsius"),
        pytest.param(US_CUSTOMARY_SYSTEM, 0.9, 0.5, id="fahrenheit"),
    ],
)
async def test_offset_number_changes_internal_target_only(
    hass, unit_system, number_value, kelvin_offset
):
    """The number changes regulation while the climate target stays unchanged."""
    hass.config.units = unit_system
    set_room_sensor(hass, 19.0)
    await build_devices(hass, GENERIC_HEAT_TRV)
    entry = make_entry(GENERIC_HEAT_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    target = bt.heat_target_temperature

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.bt_test_target_offset", "value": number_value},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert bt.target_offset == kelvin_offset
    assert bt.heat_target_temperature == target
    expected_display_target = (
        target if unit_system == METRIC_SYSTEM else target * 1.8 + 32.0
    )
    assert (
        hass.states.get(BT_ENTITY).attributes["temperature"] == expected_display_target
    )
    assert hass.states.get(BT_ENTITY).attributes["target_offset"] == number_value
    assert (
        hass.states.get(BT_ENTITY).attributes["regulation_target_temp"]
        == target + kelvin_offset
    )
    assert float(hass.states.get("number.bt_test_target_offset").state) == number_value
