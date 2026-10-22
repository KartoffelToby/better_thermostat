"""A TRV released on its own is the user's while the room's other TRVs are controlled.

Each TRV of a room has a release switch. While it is on, Better Thermostat
sends that TRV nothing, takes over nothing it reports and raises no repair
for it, and controls the room's other TRVs as before. Turned off, the switch
brings the TRV onto the room's settings with one control cycle. The switch
survives a restart, and a thermostat that starts with a TRV released writes
nothing to it on the way up. With every TRV released the room says so in
``heating_blocked_by``.
"""

import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACMode,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import Context, CoreState, Event, State
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.better_thermostat.utils.helpers import entry_issue_id

from .conftest import (
    BT_ENTITY,
    CRITICAL_GRACE,
    DOMAIN,
    SENSOR_ID,
    WRITE_BUDGET,
    build_devices,
    make_entry,
    mode_commands,
    profile_id,
    set_room_sensor,
    setpoint_commands,
    setup_entry,
    wait_for_startup,
)
from .device_profiles import (
    COOLER_ID,
    DUAL_ROLE,
    GROUP_OF_THREE,
    ROOM_AC_COOLER,
    VALVE_GROUP,
    GroupScenario,
)
from .test_released_devices import ROOM, WARM_ROOM
from .write_hold import holding_next_write, poll_until

TWO_HEADS = GroupScenario(name="two_heads", profiles=GROUP_OF_THREE.profiles[:2])
"""A room with two plain heads, the first of which the tests release."""

BY_HAND, CONTROLLED = (profile.entity_id for profile in TWO_HEADS.profiles)


async def _settle(hass, rounds=50):
    for _ in range(rounds):
        await asyncio.sleep(0)
        await hass.async_block_till_done()


async def _let_minutes_pass(hass, minutes):
    """Fire every minute's timers in turn, the way the clock would."""
    start = dt_util.utcnow()
    for minute in range(1, minutes + 1):
        async_fire_time_changed(hass, start + timedelta(minutes=minute))
        await _settle(hass, 20)


def _release_switch(hass, bt, trv_entity_id) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{bt.unique_id}_{trv_entity_id}_release"
    )
    assert entity_id is not None
    return entity_id


async def _set_release(hass, bt, trv_entity_id, released: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if released else "turn_off",
        {ATTR_ENTITY_ID: _release_switch(hass, bt, trv_entity_id)},
        blocking=True,
    )
    await _settle(hass)


async def _call(hass, service, data):
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
    )
    await _settle(hass)


def _writes_to(events, trv_entity_id) -> list[tuple[str, str, object]]:
    """Every service call that reached the TRV or one of its device's entities.

    The device's entities carry the TRV's object id: its offset, valve and
    external temperature numbers. Read off the bus, so a call Home Assistant
    turned away still counts: it was sent.
    """
    object_id = trv_entity_id.split(".", 1)[1]
    writes = []
    for event in events:
        target = event.data["service_data"].get(ATTR_ENTITY_ID)
        targets = [target] if isinstance(target, str) else list(target or [])
        if any(object_id in target for target in targets):
            writes.append(
                (
                    event.data["domain"],
                    event.data["service"],
                    event.data["service_data"],
                )
            )
    return writes


def _at_the_device(device, *, hvac_mode=None, setpoint=None):
    """Operate a device at its own controls, under a context of its own."""
    device.async_set_context(Context())
    if hvac_mode is not None:
        device._attr_hvac_mode = hvac_mode
    if setpoint is not None:
        device._attr_target_temperature = setpoint
    device.async_write_ha_state()


async def _start(hass, scenario):
    set_room_sensor(hass, 19.0)
    entry = make_entry(scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)
    return entry, bt


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_a_released_trv_gets_nothing_while_the_other_is_controlled(
    hass, trv_group
):
    """Nothing the room or the user does reaches the released TRV.

    The room's target is changed, the room temperature moves, the released
    TRV is turned down and switched off at its own controls and half an hour
    passes. The other TRV follows the room throughout; the room keeps its
    own target and mode and shows the release. Taken back, the released TRV
    is switched on and set once.
    """
    by_hand = trv_group[0]
    _, bt = await _start(hass, TWO_HEADS)
    controlled_before = hass.states.get(CONTROLLED).attributes["temperature"]

    await _set_release(hass, bt, BY_HAND, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    with patch(WRITE_BUDGET, 0.0):
        await _call(hass, SERVICE_SET_TEMPERATURE, {"temperature": 23.0})
        set_room_sensor(hass, 17.5)
        await _settle(hass)
        _at_the_device(by_hand, setpoint=12.0)
        await _settle(hass)
        _at_the_device(by_hand, hvac_mode=HVACMode.OFF)
        await _settle(hass)
        await _let_minutes_pass(hass, 30)

    assert _writes_to(events, BY_HAND) == []
    assert setpoint_commands(events, CONTROLLED)
    assert hass.states.get(CONTROLLED).attributes["temperature"] > controlled_before
    assert hass.states.get(BY_HAND).attributes["temperature"] == 12.0
    assert hass.states.get(BY_HAND).state == HVACMode.OFF
    assert bt.heat_target_temperature == 23.0
    published = hass.states.get(BT_ENTITY)
    assert published.state == HVACMode.HEAT
    assert published.attributes["temperature"] == 23.0
    assert published.attributes["released_trvs"] == [BY_HAND]
    assert published.attributes["devices_released"] is False
    assert published.attributes["active_overrides"] == ["trvs_released"]
    assert published.attributes["heating_blocked_by"] is None

    with patch(WRITE_BUDGET, 0.0):
        await _set_release(hass, bt, BY_HAND, False)
        await _let_minutes_pass(hass, 15)

    assert mode_commands(events, BY_HAND) == [HVACMode.HEAT]
    assert len(setpoint_commands(events, BY_HAND)) == 1
    assert hass.states.get(BY_HAND).state == HVACMode.HEAT
    assert (
        hass.states.get(BY_HAND).attributes["temperature"]
        == hass.states.get(CONTROLLED).attributes["temperature"]
    )
    published = hass.states.get(BT_ENTITY)
    assert published.attributes["released_trvs"] == []
    assert published.attributes["active_overrides"] == []


@pytest.mark.parametrize("trv_group", [VALVE_GROUP], indirect=True, ids=profile_id)
async def test_a_released_head_driven_by_its_valve_gets_no_position(hass, trv_group):
    """The valve channel of a released head stays the user's as well.

    The room asks for more heat, and only the head still controlled has its
    valve opened.
    """
    by_hand, controlled = (entity.entity_id for entity in trv_group.entities)
    _, bt = await _start(hass, VALVE_GROUP)
    with patch(WRITE_BUDGET, 0.0):
        await _call(hass, SERVICE_SET_TEMPERATURE, {"temperature": 15.0})

    await _set_release(hass, bt, by_hand, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    with patch(WRITE_BUDGET, 0.0):
        await _call(hass, SERVICE_SET_TEMPERATURE, {"temperature": 24.0})
        await _let_minutes_pass(hass, 30)

    assert _writes_to(events, by_hand) == []
    assert any(domain == "number" for domain, _, _ in _writes_to(events, controlled))


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_every_trv_released_blocks_the_room_like_its_release(hass, trv_group):
    """With every TRV released the room names the release as its block.

    Taking one TRV back hands the heating to it again.
    """
    _, bt = await _start(hass, TWO_HEADS)

    await _set_release(hass, bt, BY_HAND, True)
    assert hass.states.get(BT_ENTITY).attributes["heating_blocked_by"] is None
    await _set_release(hass, bt, CONTROLLED, True)

    published = hass.states.get(BT_ENTITY)
    assert published.state == HVACMode.HEAT
    assert published.attributes["heating_blocked_by"] == "devices_released"
    assert published.attributes["devices_released"] is False
    assert published.attributes["released_trvs"] == [BY_HAND, CONTROLLED]
    assert published.attributes["active_overrides"] == ["trvs_released"]

    await _set_release(hass, bt, CONTROLLED, False)
    await _let_minutes_pass(hass, 2)

    assert hass.states.get(BT_ENTITY).attributes["heating_blocked_by"] is None


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_only_the_controlled_trvs_decide_whether_the_room_is_off(hass, trv_group):
    """A released TRV heating by hand does not keep the room on.

    The room switches off once every TRV still controlled is switched off at
    its own controls, whatever the released TRV does.
    """
    by_hand, controlled = trv_group.entities
    _, bt = await _start(hass, TWO_HEADS)
    await _set_release(hass, bt, BY_HAND, True)
    _at_the_device(by_hand, hvac_mode=HVACMode.HEAT, setpoint=25.0)
    await _settle(hass)

    _at_the_device(controlled, hvac_mode=HVACMode.OFF)
    await _let_minutes_pass(hass, 2)

    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF
    assert hass.states.get(BY_HAND).state == HVACMode.HEAT


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_a_released_trv_going_away_raises_no_repair(hass, trv_group):
    """An outage of a released TRV is the user's business, the other's is not.

    A repair raised before the release is withdrawn by it, and once the TRV
    is taken back while still gone it is reported again.
    """
    by_hand, controlled = trv_group.entities
    set_room_sensor(hass, 19.0)
    entry = make_entry(TWO_HEADS)
    issue = entry_issue_id(entry.entry_id, "missing_entity", BY_HAND)
    other_issue = entry_issue_id(entry.entry_id, "missing_entity", CONTROLLED)
    with patch(CRITICAL_GRACE, timedelta(seconds=0)):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)

        by_hand.set_available(False)
        await _let_minutes_pass(hass, 2)
        assert (DOMAIN, issue) in ir.async_get(hass).issues

        await _set_release(hass, bt, BY_HAND, True)
        assert (DOMAIN, issue) not in ir.async_get(hass).issues

        controlled.set_available(False)
        await _let_minutes_pass(hass, 5)
        assert (DOMAIN, issue) not in ir.async_get(hass).issues
        assert (DOMAIN, other_issue) in ir.async_get(hass).issues
        assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT

        await _set_release(hass, bt, BY_HAND, False)
        assert (DOMAIN, issue) in ir.async_get(hass).issues


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_a_released_trv_survives_a_restart_without_a_write(hass, trv_group):
    """A thermostat that starts with a TRV released sets up the other one only.

    The release switch comes back on, a report from the released TRV sets
    nothing up, and the other TRV is controlled from the start. Taken back,
    the released TRV is set up and set once.
    """
    by_hand = trv_group[0]
    mock_restore_cache(
        hass,
        [
            State(
                BT_ENTITY,
                HVACMode.HEAT,
                {"temperature": 22.5, "released_trvs": [BY_HAND]},
            )
        ],
    )
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    hass.set_state(CoreState.starting)
    entry = make_entry(TWO_HEADS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    _at_the_device(by_hand, setpoint=18.0)
    await _let_minutes_pass(hass, 5)

    assert bt.released_trvs == {BY_HAND}
    assert hass.states.get(_release_switch(hass, bt, BY_HAND)).state == STATE_ON
    assert hass.states.get(_release_switch(hass, bt, CONTROLLED)).state == STATE_OFF
    assert _writes_to(events, BY_HAND) == []
    assert setpoint_commands(events, CONTROLLED)
    assert bt.real_trvs[BY_HAND].awaiting_initialization is True

    with patch(WRITE_BUDGET, 0.0):
        await _set_release(hass, bt, BY_HAND, False)
        await _let_minutes_pass(hass, 5)

    assert bt.real_trvs[BY_HAND].awaiting_initialization is False
    assert len(setpoint_commands(events, BY_HAND)) == 1
    assert (
        hass.states.get(BY_HAND).attributes["temperature"]
        == hass.states.get(CONTROLLED).attributes["temperature"]
    )


async def _release_during_held_write(hass, bt, held, events) -> list[Event]:
    """Release the first head while ``held`` keeps a cycle inside its write.

    Returns the bus events from the release on, once the cycle has ended.
    """
    await held.wait_reached(hass)
    await hass.services.async_call(
        "switch",
        "turn_on",
        {ATTR_ENTITY_ID: _release_switch(hass, bt, BY_HAND)},
        blocking=True,
    )
    assert bt.released_trvs == {BY_HAND}
    released_at = len(events)
    held.release()
    assert await poll_until(hass, lambda: bt.control_cycle_started_monotonic is None)
    await _settle(hass)
    return events[released_at:]


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_a_trv_released_during_its_write_gets_nothing_more(hass, trv_group):
    """The release reaches a cycle between two writes to the released TRV.

    The room is switched on with a new target, so its cycle switches each
    head on and then sets it. The first head is released while it is still
    taking its mode: it gets no setpoint, and the other head is switched on
    and set all the same.
    """
    by_hand = trv_group[0]
    _, bt = await _start(hass, TWO_HEADS)
    await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.OFF})
    await _call(hass, SERVICE_SET_TEMPERATURE, {"temperature": 24.0})
    setpoint_before = hass.states.get(BY_HAND).attributes["temperature"]
    assert hass.states.get(CONTROLLED).attributes["temperature"] == setpoint_before
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    with patch(WRITE_BUDGET, 0.0):
        async with holding_next_write(by_hand, "async_set_hvac_mode") as held:
            await hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_HVAC_MODE,
                {ATTR_ENTITY_ID: BT_ENTITY, "hvac_mode": HVACMode.HEAT},
                blocking=True,
            )
            after_release = await _release_during_held_write(hass, bt, held, events)

    assert mode_commands(events, BY_HAND) == [HVACMode.HEAT]
    assert _writes_to(after_release, BY_HAND) == []
    assert hass.states.get(BY_HAND).attributes["temperature"] == setpoint_before
    assert mode_commands(events, CONTROLLED) == [HVACMode.HEAT]
    assert setpoint_commands(events, CONTROLLED)
    assert hass.states.get(CONTROLLED).attributes["temperature"] != setpoint_before


async def test_a_room_with_its_trv_released_keeps_cooling(hass):
    """The cooler is no TRV: releasing the room's TRV leaves it controlled.

    The room is warm and switched to heating and cooling with its TRV
    released. The air conditioner is switched on to cool, and the TRV gets
    nothing. The room names the release as what keeps it from heating.
    """
    trv, cooler = await build_devices(hass, ROOM.trv, ROOM_AC_COOLER)
    set_room_sensor(hass, WARM_ROOM)
    entry = make_entry(ROOM)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.OFF})
    assert hass.states.get(COOLER_ID).state == HVACMode.OFF

    await _set_release(hass, bt, trv.entity_id, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    with patch(WRITE_BUDGET, 0.0):
        await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.HEAT_COOL})
        await _let_minutes_pass(hass, 5)

    assert mode_commands(events, COOLER_ID) == [HVACMode.COOL]
    assert hass.states.get(COOLER_ID).state == HVACMode.COOL
    assert _writes_to(events, trv.entity_id) == []
    assert (
        hass.states.get(BT_ENTITY).attributes["heating_blocked_by"]
        == "devices_released"
    )


async def test_a_device_heating_and_cooling_is_released_in_both_roles(hass):
    """A device wired as TRV and as cooler, released, gets neither role's writes."""
    (device,) = await build_devices(hass, DUAL_ROLE.trv)
    set_room_sensor(hass, WARM_ROOM)
    entry = make_entry(DUAL_ROLE)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.OFF})
    mode_before = hass.states.get(device.entity_id).state

    await _set_release(hass, bt, device.entity_id, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    with patch(WRITE_BUDGET, 0.0):
        await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.HEAT_COOL})
        await _let_minutes_pass(hass, 10)

    assert _writes_to(events, device.entity_id) == []
    assert hass.states.get(device.entity_id).state == mode_before


@pytest.mark.parametrize("trv_group", [TWO_HEADS], indirect=True, ids=profile_id)
async def test_a_restart_takes_no_target_from_a_released_trv(hass, trv_group):
    """A room restarted without a saved target reads it off its controlled TRV.

    The released TRV is set far above the other one by hand; its setpoint
    does not become the room's target.
    """
    by_hand, controlled = trv_group.entities
    _at_the_device(by_hand, setpoint=28.0)
    _at_the_device(controlled, setpoint=19.0)
    mock_restore_cache(
        hass, [State(BT_ENTITY, HVACMode.HEAT, {"released_trvs": [BY_HAND]})]
    )
    _, bt = await _start(hass, TWO_HEADS)

    assert bt.released_trvs == {BY_HAND}
    assert bt.heat_target_temperature == 19.0
    assert hass.states.get(BY_HAND).attributes["temperature"] == 28.0
