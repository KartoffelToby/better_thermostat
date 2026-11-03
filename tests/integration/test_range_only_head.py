"""A head that offers only a temperature range takes the room target.

Such a head advertises TARGET_TEMPERATURE_RANGE without TARGET_TEMPERATURE and
publishes its heating setpoint as ``target_temp_low``. Home Assistant refuses a
``temperature`` write to it, so the setpoint has to travel as the lower bound
of the range, with the upper bound the head already holds. The value on the
wire is the calibrated setpoint BT computes for the room target, which it
keeps as the head's ``last_temperature``.
"""

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    ClimateEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.setup import async_setup_component
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


class _RangeHead(FakeTrvEntity):
    """A heating head on an 18-25 °C band with no single setpoint."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, *, low=18.0, high=25.0):
        super().__init__()
        self._attr_target_temperature = None
        self._attr_target_temperature_low = low
        self._attr_target_temperature_high = high
        self.range_writes: list[dict] = []

    async def async_set_temperature(self, **kwargs) -> None:
        """Apply and confirm a range write, recording what arrived."""
        assert ATTR_TEMPERATURE not in kwargs
        self.range_writes.append(
            {k: v for k, v in kwargs.items() if k != ATTR_ENTITY_ID}
        )
        if ATTR_TARGET_TEMP_LOW in kwargs:
            self._attr_target_temperature_low = kwargs[ATTR_TARGET_TEMP_LOW]
        if ATTR_TARGET_TEMP_HIGH in kwargs:
            self._attr_target_temperature_high = kwargs[ATTR_TARGET_TEMP_HIGH]
        self.async_write_ha_state()


async def _start(hass, head):
    setup_test_component_platform(hass, CLIMATE_DOMAIN, [head])
    assert await async_setup_component(
        hass, CLIMATE_DOMAIN, {CLIMATE_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    hass.states.async_set(SENSOR_ID, "19.0", {"unit_of_measurement": "°C"})
    entry = make_entry()
    await setup_entry(hass, entry)
    return await wait_for_startup(hass, entry)


def _head_low(hass):
    return hass.states.get(TRV_ID).attributes["target_temp_low"]


async def _set_room_target(hass, value):
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {ATTR_ENTITY_ID: BT_ENTITY, ATTR_TEMPERATURE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_a_range_only_head_takes_the_room_target_as_its_lower_bound(hass):
    """A new room target reaches the head as target_temp_low; the band's top stays."""
    head = _RangeHead()
    bt = await _start(hass, head)

    await _set_room_target(hass, 22.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(hass, lambda: _head_low(hass) >= 22.0)
    assert _head_low(hass) == trv.last_temperature
    assert head.range_writes[-1] == {
        ATTR_TARGET_TEMP_LOW: trv.last_temperature,
        ATTR_TARGET_TEMP_HIGH: 25.0,
    }
    assert bt.bt_target_temp == 22.0


async def test_a_range_only_head_confirms_the_write(hass):
    """The head's echo on target_temp_low confirms the write and moves nothing."""
    head = _RangeHead()
    bt = await _start(hass, head)

    await _set_room_target(hass, 22.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(hass, lambda: _head_low(hass) >= 22.0)
    assert await wait_for(hass, lambda: trv.target_temp_received is True)
    assert bt.bt_target_temp == 22.0
    writes = len(head.range_writes)
    await _set_room_target(hass, 22.0)
    assert len(head.range_writes) == writes
    assert bt.bt_target_temp == 22.0


async def test_a_range_only_head_raises_a_top_below_the_new_lower_bound(hass):
    """A room target above the band's top lifts the top with it."""
    head = _RangeHead(low=18.0, high=20.0)
    bt = await _start(hass, head)

    await _set_room_target(hass, 23.0)

    trv = bt.real_trvs[TRV_ID]
    assert await wait_for(hass, lambda: _head_low(hass) >= 23.0)
    assert _head_low(hass) == trv.last_temperature
    assert head.range_writes[-1] == {
        ATTR_TARGET_TEMP_LOW: trv.last_temperature,
        ATTR_TARGET_TEMP_HIGH: trv.last_temperature,
    }
    assert bt.bt_target_temp == 23.0
