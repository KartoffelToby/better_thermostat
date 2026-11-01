"""A TRV whose published range depends on its mode is driven in the range it heats in.

HomeKit Controller publishes Home Assistant's default range of 7 to 35 °C for
a thermostat that is off and the device's own range once it heats, and Home
Assistant refuses a setpoint outside the range of the mode the device is in.
Better Thermostat started while such a device was off reads the wide range,
so the range the device publishes once it heats has to replace it.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import Context, callback
from homeassistant.helpers.event import async_call_later
import pytest

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import GENERIC_HEAT_TRV, TRV_ID

OFF_RANGE = (7.0, 35.0)
"""The range Home Assistant publishes for a HomeKit thermostat that is off."""

HEAT_RANGE = (5.0, 25.0)
"""The range the device publishes once it heats."""

HOMEKIT_TRV = replace(
    GENERIC_HEAT_TRV,
    name="homekit_trv",
    hvac_mode=HVACMode.OFF,
    min_temp=OFF_RANGE[0],
    max_temp=OFF_RANGE[1],
)


def _publish_range_by_mode(fake_trv) -> None:
    """Make the fake device publish the range of the mode it is switched to."""
    set_hvac_mode = fake_trv.async_set_hvac_mode

    async def switching_range(hvac_mode) -> None:
        low, high = OFF_RANGE if hvac_mode == HVACMode.OFF else HEAT_RANGE
        fake_trv._attr_min_temp = low
        fake_trv._attr_max_temp = high
        await set_hvac_mode(hvac_mode)

    fake_trv.async_set_hvac_mode = switching_range


def _publish_range_by_mode_later(hass, fake_trv) -> None:
    """Make the fake device publish its new range in a report after the mode's.

    The later report repeats the mode and the setpoint the device holds, and
    it arrives inside the window in which Home Assistant stamps the device's
    states with the context of BT's write.
    """
    set_hvac_mode = fake_trv.async_set_hvac_mode

    async def switching_range_later(hvac_mode) -> None:
        await set_hvac_mode(hvac_mode)

        @callback
        def publish_range(_now) -> None:
            low, high = OFF_RANGE if hvac_mode == HVACMode.OFF else HEAT_RANGE
            fake_trv._attr_min_temp = low
            fake_trv._attr_max_temp = high
            fake_trv.async_write_ha_state()

        async_call_later(hass, 0.1, publish_range)

    fake_trv.async_set_hvac_mode = switching_range_later


async def _start(hass, fake_trv, *, range_later: bool = False):
    if range_later:
        _publish_range_by_mode_later(hass, fake_trv)
    else:
        _publish_range_by_mode(fake_trv)
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
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


def _report_from_the_device(fake_trv, hvac_mode, low, high, **changes) -> None:
    """Publish a report the device sends on its own, outside BT's context."""
    fake_trv.async_set_context(Context())
    fake_trv._attr_hvac_mode = hvac_mode
    fake_trv._attr_min_temp = low
    fake_trv._attr_max_temp = high
    for name, value in changes.items():
        setattr(fake_trv, f"_attr_{name}", value)
    fake_trv.async_write_ha_state()


async def _switch_trv_off(hass, fake_trv) -> None:
    await fake_trv.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()


@pytest.mark.parametrize("fake_trv", [HOMEKIT_TRV], indirect=True)
async def test_the_range_published_while_heating_replaces_the_one_read_while_off(
    hass, fake_trv
):
    """The device's range, not the off default, bounds what reaches it."""
    bt = await _start(hass, fake_trv)
    trv = bt.real_trvs[TRV_ID]
    assert (trv.min_temp, trv.max_temp) == OFF_RANGE

    await _heat_at(hass, 30.0)

    assert await wait_for(hass, lambda: fake_trv.hvac_mode == HVACMode.HEAT)
    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)
    assert await wait_for(hass, lambda: fake_trv.target_temperature == HEAT_RANGE[1])
    assert all(
        call <= HEAT_RANGE[1]
        for call in fake_trv.set_temperature_calls
        if isinstance(call, float)
    )


@pytest.mark.parametrize("fake_trv", [HOMEKIT_TRV], indirect=True)
async def test_a_report_while_off_leaves_the_heating_range_in_place(hass, fake_trv):
    """The off default a device publishes again later does not widen the range."""
    bt = await _start(hass, fake_trv)
    trv = bt.real_trvs[TRV_ID]
    await _heat_at(hass, 21.0)
    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)

    await _switch_trv_off(hass, fake_trv)

    assert hass.states.get(TRV_ID).attributes["max_temp"] == OFF_RANGE[1]
    assert (trv.min_temp, trv.max_temp) == HEAT_RANGE


@pytest.mark.parametrize("fake_trv", [HOMEKIT_TRV], indirect=True)
async def test_a_range_published_after_the_setpoint_brings_the_setpoint_into_it(
    hass, fake_trv
):
    """A setpoint the wide range let through is rewritten once the range narrows."""
    bt = await _start(hass, fake_trv, range_later=True)
    trv = bt.real_trvs[TRV_ID]

    await _heat_at(hass, 30.0)
    assert await wait_for(hass, lambda: fake_trv.target_temperature > HEAT_RANGE[1])

    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)
    assert await wait_for(hass, lambda: fake_trv.target_temperature == HEAT_RANGE[1])


@pytest.mark.parametrize("fake_trv", [HOMEKIT_TRV], indirect=True)
async def test_a_report_in_a_mode_without_a_range_of_its_own_leaves_it_in_place(
    hass, fake_trv
):
    """The default published in a mode other than heat does not widen the range."""
    bt = await _start(hass, fake_trv)
    trv = bt.real_trvs[TRV_ID]
    await _heat_at(hass, 21.0)
    assert await wait_for(hass, lambda: (trv.min_temp, trv.max_temp) == HEAT_RANGE)

    _report_from_the_device(fake_trv, HVACMode.AUTO, *OFF_RANGE)
    await hass.async_block_till_done()

    assert (trv.min_temp, trv.max_temp) == HEAT_RANGE


PROMPT_WRITE_SECONDS = 0.5
"""How long a write the report itself asked for may take to reach the device."""

HOMEKIT_TRV_HEATING = replace(HOMEKIT_TRV, hvac_mode=HVACMode.HEAT)
"""The same device already heating at startup, still publishing the wide range."""


@pytest.mark.parametrize("fake_trv", [HOMEKIT_TRV_HEATING], indirect=True)
@pytest.mark.parametrize("during_cycle", [True, False], ids=["during", "after"])
@patch(WRITE_BUDGET, 0.0)
async def test_a_report_that_only_narrows_the_range_brings_the_setpoint_into_it(
    hass, fake_trv, during_cycle
):
    """A report from the device that changes nothing but its range is acted on.

    It arrives either while the cycle that wrote the setpoint is still running,
    which holds the report until the cycle ends, or after it. The write budget
    is lifted, so the write that follows the room target goes out at once
    instead of waiting out the interval since the startup write.
    """
    bt = await _start(hass, fake_trv)
    trv = bt.real_trvs[TRV_ID]
    await _heat_at(hass, 30.0)
    assert await wait_for(hass, lambda: fake_trv.target_temperature > HEAT_RANGE[1])
    assert await wait_for(hass, lambda: bt.ignore_states is during_cycle)

    _report_from_the_device(fake_trv, HVACMode.HEAT, *HEAT_RANGE)

    # Well inside the interval of the periodic cycle, so the write is the one
    # the report asked for.
    assert await wait_for(
        hass,
        lambda: fake_trv.target_temperature == HEAT_RANGE[1],
        timeout_seconds=PROMPT_WRITE_SECONDS,
    )
    assert (trv.min_temp, trv.max_temp) == HEAT_RANGE
