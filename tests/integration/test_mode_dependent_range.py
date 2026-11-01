"""A TRV whose published range depends on its mode is driven in the range it heats in.

HomeKit Controller publishes Home Assistant's default range of 7 to 35 °C for
a thermostat that is off and the device's own range once it heats, and Home
Assistant refuses a setpoint outside the range of the mode the device is in.
Better Thermostat started while such a device was off reads the wide range,
so the range the device publishes once it heats has to replace it.
"""

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import Context, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import setup_test_component_platform

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

OFF_RANGE = (7.0, 35.0)
"""The range Home Assistant publishes for a HomeKit thermostat that is off."""

HEAT_RANGE = (5.0, 25.0)
"""The range the device publishes once it heats."""


class _HomeKitTrv(FakeTrvEntity):
    """A TRV publishing the range of the mode it is in.

    With ``range_later`` the new range arrives in a report after the mode's,
    inside the window in which Home Assistant stamps the device's states with
    the context of BT's write.
    """

    def __init__(self, hvac_mode=HVACMode.OFF, *, range_later=False):
        super().__init__()
        self._attr_hvac_mode = hvac_mode
        self._attr_min_temp, self._attr_max_temp = OFF_RANGE
        self.range_later = range_later

    def _publish_range_of(self, hvac_mode) -> None:
        low, high = OFF_RANGE if hvac_mode == HVACMode.OFF else HEAT_RANGE
        self._attr_min_temp = low
        self._attr_max_temp = high

    async def async_set_hvac_mode(self, hvac_mode) -> None:
        """Apply a mode write with the range that mode publishes."""
        if not self.range_later:
            self._publish_range_of(hvac_mode)
            await super().async_set_hvac_mode(hvac_mode)
            return
        await super().async_set_hvac_mode(hvac_mode)

        @callback
        def publish_range(_now) -> None:
            self._publish_range_of(hvac_mode)
            self.async_write_ha_state()

        async_call_later(self.hass, 0.1, publish_range)


async def _start(hass, trv):
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [trv])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


async def _heat_at(hass, target: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: BT_ENTITY, ATTR_TEMPERATURE: target},
        blocking=True,
    )
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.HEAT},
        blocking=True,
    )
    await hass.async_block_till_done()


def _report_from_the_device(trv, hvac_mode, low, high) -> None:
    """Publish a report the device sends on its own, outside BT's context."""
    trv.async_set_context(Context())
    trv._attr_hvac_mode = hvac_mode
    trv._attr_min_temp = low
    trv._attr_max_temp = high
    trv.async_write_ha_state()


async def test_the_range_published_while_heating_replaces_the_one_read_while_off(hass):
    """The device's range, not the off default, bounds what reaches it."""
    device = _HomeKitTrv()
    bt = await _start(hass, device)
    trv = bt.real_trvs[TRV_ID]
    assert (trv.min_temp, trv.max_temp) == OFF_RANGE

    await _heat_at(hass, 30.0)

    assert await wait_for(hass, lambda: device.hvac_mode == HVACMode.HEAT)
    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)
    assert await wait_for(hass, lambda: device.target_temperature == HEAT_RANGE[1])
    assert all(call <= HEAT_RANGE[1] for call in device.set_temperature_calls)


async def test_a_report_while_off_leaves_the_heating_range_in_place(hass):
    """The off default a device publishes again later does not widen the range."""
    device = _HomeKitTrv()
    bt = await _start(hass, device)
    trv = bt.real_trvs[TRV_ID]
    await _heat_at(hass, 21.0)
    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)

    await device.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()

    assert hass.states.get(TRV_ID).attributes["max_temp"] == OFF_RANGE[1]
    assert (trv.min_temp, trv.max_temp) == HEAT_RANGE


async def test_a_range_published_after_the_setpoint_brings_the_setpoint_into_it(hass):
    """A setpoint the wide range let through is rewritten once the range narrows."""
    device = _HomeKitTrv(range_later=True)
    bt = await _start(hass, device)
    trv = bt.real_trvs[TRV_ID]

    await _heat_at(hass, 30.0)
    assert await wait_for(hass, lambda: device.target_temperature > HEAT_RANGE[1])

    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)
    assert await wait_for(hass, lambda: device.target_temperature == HEAT_RANGE[1])


async def test_a_report_in_a_mode_without_a_range_of_its_own_leaves_it_in_place(hass):
    """The default published in a mode other than heat does not widen the range."""
    device = _HomeKitTrv()
    device._attr_hvac_modes = [HVACMode.HEAT, HVACMode.AUTO, HVACMode.OFF]
    bt = await _start(hass, device)
    trv = bt.real_trvs[TRV_ID]
    await _heat_at(hass, 21.0)
    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)

    _report_from_the_device(device, HVACMode.AUTO, *OFF_RANGE)
    await hass.async_block_till_done()

    assert (trv.min_temp, trv.max_temp) == HEAT_RANGE


PROMPT_WRITE_SECONDS = 0.5
"""How long a write the report itself asked for may take to reach the device."""


@pytest.mark.parametrize("during_cycle", [True, False], ids=["during", "after"])
async def test_a_report_that_only_narrows_the_range_brings_the_setpoint_into_it(
    hass, during_cycle
):
    """A report from the device that changes nothing but its range is acted on.

    It arrives either while the cycle that wrote the setpoint is still running,
    which holds the report until the cycle ends, or after it.
    """
    device = _HomeKitTrv(HVACMode.HEAT)
    bt = await _start(hass, device)
    trv = bt.real_trvs[TRV_ID]
    await _heat_at(hass, 30.0)
    assert await wait_for(hass, lambda: device.target_temperature > HEAT_RANGE[1])
    assert await wait_for(hass, lambda: bt.ignore_states is during_cycle)

    _report_from_the_device(device, HVACMode.HEAT, *HEAT_RANGE)

    # Well inside the interval of the periodic cycle, so the write is the one
    # the report asked for.
    assert await wait_for(
        hass,
        lambda: device.target_temperature == HEAT_RANGE[1],
        timeout_s=PROMPT_WRITE_SECONDS,
    )
    assert (trv.min_temp, trv.max_temp) == HEAT_RANGE
