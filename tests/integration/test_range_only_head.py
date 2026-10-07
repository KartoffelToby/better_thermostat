"""A head that offers only a temperature range takes the room target.

Such a head advertises TARGET_TEMPERATURE_RANGE without TARGET_TEMPERATURE and
publishes its heating setpoint as ``target_temp_low``. Home Assistant refuses a
``temperature`` write to it, so the setpoint has to travel as the lower bound
of the range, with the upper bound the head already holds. The value on the
wire is the calibrated setpoint BT computes for the room target, which it
keeps as the head's ``commanded_setpoint``.

Every test moves the room target after startup, so the write under test is
the one that follows a user's change rather than the startup write. The write
budget is lifted, so that write goes out at once instead of waiting out the
interval since the startup write.
"""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
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
from .device_profiles import RANGE_ONLY_HEAT_TRV, TRV_ID

LOW_TOP = replace(RANGE_ONLY_HEAT_TRV, target_temperature_high=20.0)
"""The same head with the top of its band at 20 °C."""


@pytest.fixture(autouse=True)
def _no_write_budget():
    """Let a write follow the previous one without waiting out the interval."""
    with patch(WRITE_BUDGET, 0.0):
        yield


async def _start(hass, fake_trv):
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


def _head_low(hass):
    return hass.states.get(TRV_ID).attributes[ATTR_TARGET_TEMP_LOW]


async def _set_room_target(hass, value):
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: BT_ENTITY, ATTR_TEMPERATURE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("fake_trv", [RANGE_ONLY_HEAT_TRV], indirect=True)
async def test_a_range_only_head_takes_the_room_target_as_its_lower_bound(
    hass, fake_trv
):
    """A new room target reaches the head as target_temp_low; the band's top stays."""
    bt = await _start(hass, fake_trv)

    await _set_room_target(hass, 22.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(hass, lambda: _head_low(hass) >= 22.0)
    assert _head_low(hass) == trv.commanded_setpoint
    assert fake_trv.set_temperature_calls[-1] == {
        ATTR_TARGET_TEMP_LOW: trv.commanded_setpoint,
        ATTR_TARGET_TEMP_HIGH: 25.0,
    }
    assert bt.heat_target_temperature == 22.0


@pytest.mark.parametrize("fake_trv", [RANGE_ONLY_HEAT_TRV], indirect=True)
async def test_a_range_only_head_confirms_the_write(hass, fake_trv):
    """The head's echo on target_temp_low confirms the write and moves nothing."""
    bt = await _start(hass, fake_trv)

    await _set_room_target(hass, 22.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(hass, lambda: _head_low(hass) >= 22.0)
    assert await wait_for(hass, lambda: trv.target_temp_received is True)
    assert trv.confirmed_setpoint == trv.commanded_setpoint
    assert bt.heat_target_temperature == 22.0
    writes = len(fake_trv.set_temperature_calls)
    await _set_room_target(hass, 22.0)
    assert len(fake_trv.set_temperature_calls) == writes
    assert bt.heat_target_temperature == 22.0


@pytest.mark.parametrize("fake_trv", [LOW_TOP], indirect=True)
async def test_a_range_only_head_raises_a_top_below_the_new_lower_bound(hass, fake_trv):
    """A room target above the band's top lifts the top with it."""
    bt = await _start(hass, fake_trv)

    await _set_room_target(hass, 23.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(hass, lambda: _head_low(hass) >= 23.0)
    assert _head_low(hass) == trv.commanded_setpoint
    assert fake_trv.set_temperature_calls[-1] == {
        ATTR_TARGET_TEMP_LOW: trv.commanded_setpoint,
        ATTR_TARGET_TEMP_HIGH: trv.commanded_setpoint,
    }
    assert bt.heat_target_temperature == 23.0
