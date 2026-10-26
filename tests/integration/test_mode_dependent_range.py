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
from .device_profiles import (
    COOLER_ID,
    GENERIC_HEAT_TRV,
    ROOM_AC_COOLER,
    TRV_ID,
    GroupScenario,
    RoleScenario,
)

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


@pytest.mark.parametrize("fake_trv", [HOMEKIT_TRV], indirect=True)
async def test_the_rooms_heating_range_follows_the_range_the_trv_heats_in(
    hass, fake_trv
):
    """The room's range and target are bounded by the TRV's heating range."""
    bt = await _start(hass, fake_trv)
    assert (bt.bt_min_temp, bt.bt_max_temp) == OFF_RANGE

    await _heat_at(hass, 30.0)

    assert await wait_for(hass, lambda: (bt.bt_min_temp, bt.bt_max_temp) == HEAT_RANGE)
    assert bt.heat_target_temperature == HEAT_RANGE[1]


def _homekit_head(letter: str):
    return replace(
        HOMEKIT_TRV,
        name=f"homekit_trv_{letter}",
        entity_id=f"climate.homekit_trv_{letter}",
        entity_name=f"homekit trv {letter}",
    )


WIDER_TOP = 28.0
"""A top above the heating range and below the off default."""

HOMEKIT_PAIR = GroupScenario(
    name="homekit_pair", profiles=(_homekit_head("a"), _homekit_head("b"))
)
"""Two HomeKit-style heads in one room, both off at startup."""


@pytest.mark.parametrize("trv_group", [HOMEKIT_PAIR], indirect=True)
async def test_a_head_switched_off_keeps_its_heating_range_in_the_rooms(
    hass, trv_group
):
    """A head that is off counts with the range it heats in, not the off default.

    The other head widens its range, so both of the off head's bounds are
    the ones that bound the room.
    """
    head_a, head_b = trv_group.entities
    for head in (head_a, head_b):
        _publish_range_by_mode(head)
    set_room_sensor(hass, 19.0)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _heat_at(hass, 21.0)
    assert await wait_for(hass, lambda: (bt.bt_min_temp, bt.bt_max_temp) == HEAT_RANGE)

    _report_from_the_device(head_a, HVACMode.OFF, *OFF_RANGE)
    await hass.async_block_till_done()
    _report_from_the_device(head_b, HVACMode.HEAT, HEAT_RANGE[0], WIDER_TOP)

    trv_b = bt.real_trvs[head_b.entity_id]
    assert await wait_for(hass, lambda: trv_b.max_temp == WIDER_TOP)
    assert (bt.bt_min_temp, bt.bt_max_temp) == HEAT_RANGE


HOMEKIT_ROOM_WITH_COOLER = RoleScenario(
    name="homekit_room_with_cooler",
    trv=HOMEKIT_TRV,
    cooler=ROOM_AC_COOLER,
    cooler_entity_id=COOLER_ID,
)
"""A HomeKit-style head and a separate cooler that is off."""

COOLER_RANGE = (ROOM_AC_COOLER.min_temp, ROOM_AC_COOLER.max_temp)


@pytest.mark.parametrize("device_role", [HOMEKIT_ROOM_WITH_COOLER], indirect=True)
async def test_a_trvs_heating_range_leaves_the_coolers_range_alone(hass, device_role):
    """The heating range is bounded anew without reading the cooler again.

    The cooler publishes another range while it is off, as an integration
    may; the range it was read with stays the cooling channel's.
    """
    _publish_range_by_mode(device_role.thermostat)
    set_room_sensor(hass, 19.0)
    entry = make_entry(device_role.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == COOLER_RANGE

    _report_from_the_device(device_role.cooler, HVACMode.OFF, *OFF_RANGE)
    await hass.async_block_till_done()
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.HEAT_COOL},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert await wait_for(hass, lambda: (bt.bt_min_temp, bt.bt_max_temp) == HEAT_RANGE)
    assert (bt.cool_min_temperature, bt.cool_max_temperature) == COOLER_RANGE


HOMEKIT_PAIR_HEATING = GroupScenario(
    name="homekit_pair_heating",
    profiles=tuple(
        replace(
            head,
            hvac_mode=HVACMode.HEAT,
            min_temp=HEAT_RANGE[0],
            max_temp=HEAT_RANGE[1],
        )
        for head in HOMEKIT_PAIR.profiles
    ),
)
"""The same two heads, both heating at startup."""


@pytest.mark.parametrize("trv_group", [HOMEKIT_PAIR_HEATING], indirect=True)
async def test_a_head_heating_at_startup_keeps_that_range_in_the_rooms(hass, trv_group):
    """The range a head heats in at startup bounds the room once it is off."""
    head_a, head_b = trv_group.entities
    for head in (head_a, head_b):
        _publish_range_by_mode(head)
    set_room_sensor(hass, 19.0)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert (bt.bt_min_temp, bt.bt_max_temp) == HEAT_RANGE

    _report_from_the_device(head_a, HVACMode.OFF, *OFF_RANGE)
    await hass.async_block_till_done()
    _report_from_the_device(head_b, HVACMode.HEAT, HEAT_RANGE[0], WIDER_TOP)

    trv_b = bt.real_trvs[head_b.entity_id]
    assert await wait_for(hass, lambda: trv_b.max_temp == WIDER_TOP)
    assert (bt.bt_min_temp, bt.bt_max_temp) == HEAT_RANGE
