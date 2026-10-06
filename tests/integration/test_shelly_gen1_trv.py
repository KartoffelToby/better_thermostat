"""A Shelly TRV (Gen1) closed by Better Thermostat stays switched on.

Home Assistant's Shelly integration reports this TRV as off whenever its
setpoint sits at the minimum of 4 °C. The simulated device here does the
same, so a setpoint written at the minimum reads back as the TRV being
switched off at the device.
"""

from homeassistant.components.climate.const import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    setup_test_component_platform,
)

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
MODEL = "SHTRV-01"


class ShellyGen1Trv(FakeTrvEntity):
    """A TRV whose mode Home Assistant derives from its setpoint."""

    _attr_min_temp = 4.0
    _attr_max_temp = 31.0

    def __init__(self):
        """Start the TRV heating at 21 °C."""
        super().__init__()
        self._attr_target_temperature = 21.0

    async def async_set_temperature(self, **kwargs) -> None:
        """Apply the setpoint and report off when it sits at the minimum."""
        await super().async_set_temperature(**kwargs)
        self._attr_hvac_mode = (
            HVACMode.OFF
            if kwargs[ATTR_TEMPERATURE] <= self._attr_min_temp
            else HVACMode.HEAT
        )
        self.async_write_ha_state()


@pytest.fixture
async def shelly_trv(hass):
    """Register the simulated Shelly TRV with the real climate component."""
    entity = ShellyGen1Trv()
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [entity])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(TRV_ID) is not None
    return entity


def _shelly_entry():
    """A config entry naming the TRV's model, as the config flow stores it."""
    template = make_entry()
    data = {**template.data, "model": MODEL}
    data["thermostat"] = [
        {**template.data["thermostat"][0], "model": MODEL, "integration": "shelly"}
    ]
    return MockConfigEntry(
        domain=template.domain,
        version=template.version,
        data=data,
        title=template.title,
    )


async def test_closing_the_valve_does_not_switch_the_room_off(hass, shelly_trv):
    """The lowest setpoint written sits one step above the minimum.

    The room is far above a target at the bottom of the range, so the
    calibration asks for the lowest setpoint the TRV takes. Written at the
    minimum, it would switch the TRV off.
    """
    hass.states.async_set(SENSOR_ID, "23.0", {"unit_of_measurement": "°C"})
    entry = _shelly_entry()
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.real_trvs[TRV_ID].min_temp == pytest.approx(4.5)

    baseline = len(shelly_trv.set_temperature_calls)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": bt.min_temp},
        blocking=True,
    )
    assert await wait_for(
        hass, lambda: len(shelly_trv.set_temperature_calls) > baseline
    )
    await hass.async_block_till_done()

    assert shelly_trv.set_temperature_calls[-1] == pytest.approx(4.5)
    assert shelly_trv.hvac_mode == HVACMode.HEAT
    assert bt.hvac_mode == HVACMode.HEAT
