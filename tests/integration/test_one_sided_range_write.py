"""Moving only the heating target leaves the heating/cooling pair ordered.

With a cooler configured the thermostat drives a pair of targets, and the
temperature of the active preset moves the heating one alone. The pair left
behind is what the room runs on as soon as it is in HEAT_COOL, so the
cooling target has to sit above the heating target whichever mode the room
was in when the heating target moved.
"""

from homeassistant.components.climate import HVACMode
from homeassistant.const import ATTR_ENTITY_ID
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import SEPARATE_COOLER

COMFORT_NUMBER = "number.bt_test_comfort_min"


async def _call(hass, domain, service, data):
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


@pytest.mark.parametrize("device_role", [SEPARATE_COOLER], indirect=True, ids=str)
@pytest.mark.parametrize("mode", [HVACMode.OFF, HVACMode.HEAT_COOL], ids=str)
async def test_a_heating_target_above_the_cooling_one_leaves_the_pair_ordered(
    hass, device_role, mode
):
    """The active preset moves the heating target above the cooling one."""
    set_room_sensor(hass, 22.0)
    data = dict(make_entry(SEPARATE_COOLER).data)
    data["presets"] = ["comfort"]
    entry = MockConfigEntry(domain=DOMAIN, version=18, data=data, title=data["name"])
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(
        hass,
        "climate",
        "set_temperature",
        {
            ATTR_ENTITY_ID: BT_ENTITY,
            "target_temp_low": 21.0,
            "target_temp_high": 24.0,
            "hvac_mode": mode,
        },
    )
    await _call(
        hass,
        "climate",
        "set_preset_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, "preset_mode": "comfort"},
    )
    assert bt.cool_target_temperature == 24.0

    await _call(
        hass, "number", "set_value", {ATTR_ENTITY_ID: COMFORT_NUMBER, "value": 26.0}
    )
    await _call(
        hass,
        "climate",
        "set_hvac_mode",
        {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.HEAT_COOL},
    )

    assert bt.bt_target_temp == 26.0
    assert bt.cool_target_temperature > bt.bt_target_temp
