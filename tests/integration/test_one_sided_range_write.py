"""Moving only the heating target leaves the heating/cooling pair ordered.

With a cooler configured the thermostat drives a pair of targets, and the
temperature of the active preset moves the heating one alone. The pair left
behind is what the room runs on as soon as it is in HEAT_COOL, so the
cooling target has to sit above the heating target whichever mode the room
was in when the heating target moved.
"""

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN, HVACMode
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

from .conftest import (
    DOMAIN,
    SENSOR_ID,
    FakeTrvEntity,
    make_entry,
    setup_entry,
    wait_for_startup,
)

BT_ENTITY = "climate.bt_test"
COOLER_ID = "climate.fake_cooler"
COMFORT_NUMBER = "number.bt_test_comfort_min"


class _Cooler(FakeTrvEntity):
    """A separate air conditioner with a narrower range."""

    _attr_name = "fake cooler"
    _attr_hvac_modes = [HVACMode.COOL, HVACMode.OFF]
    _attr_min_temp = 16.0

    def __init__(self):
        super().__init__()
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_current_temperature = 22.0
        self._attr_target_temperature = 24.0


async def _call(hass, domain, service, data):
    await hass.services.async_call(domain, service, data, blocking=True)
    await hass.async_block_till_done()


@pytest.mark.parametrize("mode", [HVACMode.OFF, HVACMode.HEAT_COOL], ids=str)
async def test_a_heating_target_above_the_cooling_one_leaves_the_pair_ordered(
    hass, mode
):
    """The active preset moves the heating target above the cooling one."""
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [FakeTrvEntity(), _Cooler()])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    hass.states.async_set(SENSOR_ID, "22.0", {"unit_of_measurement": "°C"})
    base = make_entry()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=base.version,
        data={**base.data, "cooler": COOLER_ID, "presets": ["comfort"]},
        title=base.title,
    )
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
    assert bt.bt_target_cooltemp == 24.0

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
    assert bt.bt_target_cooltemp > bt.bt_target_temp
