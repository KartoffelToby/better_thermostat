"""The Release devices switch hands a room's devices to the user and back.

While the switch is on, Better Thermostat sends the room's devices nothing,
the cooler included, takes over nothing they report and raises no repair
for one that goes away. The thermostat stays available and keeps its own
mode and targets, which can still be changed. Turned off, the switch hands
control back with one control cycle that brings the devices onto the room's
own settings. The switch survives a restart, and a thermostat that starts
with its devices released writes nothing to them on the way up either.
"""

import asyncio
from dataclasses import replace
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
from homeassistant.core import Context, CoreState, State
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
    EXTERNAL_INPUT_TRVZB,
    GENERIC_HEAT_TRV,
    MQTT_OFFSET_TRV,
    ROOM_AC_COOLER,
    SEPARATE_COOLER,
    TRV_ID,
)

ROOM = replace(
    SEPARATE_COOLER,
    trv=replace(SEPARATE_COOLER.trv, calibration_mode="heating_power_calibration"),
)
"""A heated room with an air conditioner, on the default calibration."""

WARM_ROOM = 27.0
"""Above the cooler's setpoint by more than any tolerance: it should cool."""


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


def _release_switch(hass, bt) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{bt.unique_id}_release_devices"
    )
    assert entity_id is not None
    return entity_id


async def _set_release(hass, bt, released: bool) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on" if released else "turn_off",
        {ATTR_ENTITY_ID: _release_switch(hass, bt)},
        blocking=True,
    )
    await _settle(hass)


async def _call(hass, service, data):
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: BT_ENTITY} | data, blocking=True
    )
    await _settle(hass)


def _device_writes(events, bt) -> list[tuple[str, str, object]]:
    """Every service call that reached a device rather than the thermostat.

    Read off the bus, so a call Home Assistant turned away still counts: it
    was sent. The thermostat's own entities are what the test itself calls.
    """
    own = {BT_ENTITY, _release_switch(bt.hass, bt)}
    writes = []
    for event in events:
        target = event.data["service_data"].get(ATTR_ENTITY_ID)
        targets = [target] if isinstance(target, str) else list(target or [])
        if not targets or set(targets) <= own:
            continue
        writes.append(
            (event.data["domain"], event.data["service"], event.data["service_data"])
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


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, EXTERNAL_INPUT_TRVZB], indirect=True, ids=profile_id
)
async def test_a_released_trv_is_the_users_until_it_is_taken_back(hass, fake_trv):
    """Nothing the room or the user does reaches a released TRV.

    The room's target is changed on the thermostat, the room temperature
    moves, the TRV is turned down and switched off at its own controls and
    half an hour passes. The thermostat keeps its own target and mode
    throughout and shows the release. Taken back, the TRV is switched on
    and set once, and nothing more follows.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _settle(hass)

    await _set_release(hass, bt, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    await _call(hass, SERVICE_SET_TEMPERATURE, {"temperature": 23.0})
    set_room_sensor(hass, 17.5)
    await _settle(hass)
    _at_the_device(fake_trv, setpoint=12.0)
    await _settle(hass)
    _at_the_device(fake_trv, hvac_mode=HVACMode.OFF)
    await _settle(hass)
    await _let_minutes_pass(hass, 30)

    assert _device_writes(events, bt) == []
    assert bt.heat_target_temperature == 23.0
    assert bt.bt_hvac_mode == HVACMode.HEAT
    published = hass.states.get(BT_ENTITY)
    assert published.state == HVACMode.HEAT
    assert published.attributes["devices_released"] is True
    assert published.attributes["temperature"] == 23.0

    with patch(WRITE_BUDGET, 0.0):
        await _set_release(hass, bt, False)
        await _let_minutes_pass(hass, 15)

    assert mode_commands(events, TRV_ID) == [HVACMode.HEAT]
    setpoints = setpoint_commands(events, TRV_ID)
    assert len(setpoints) == 1
    assert hass.states.get(TRV_ID).state == HVACMode.HEAT
    assert hass.states.get(BT_ENTITY).attributes["devices_released"] is False


async def test_a_trv_switched_on_while_the_room_is_off_leaves_the_room_off(
    hass, fake_trv
):
    """Running a released TRV by hand does not switch the room on.

    Taken back, the TRV is switched off once, the room's own mode.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.OFF})
    assert hass.states.get(TRV_ID).state == HVACMode.OFF

    await _set_release(hass, bt, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    _at_the_device(fake_trv, hvac_mode=HVACMode.HEAT, setpoint=24.0)
    await _settle(hass)
    set_room_sensor(hass, 18.0)
    await _let_minutes_pass(hass, 10)

    assert _device_writes(events, bt) == []
    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF

    await _set_release(hass, bt, False)
    await _let_minutes_pass(hass, 10)

    assert mode_commands(events, TRV_ID) == [HVACMode.OFF]
    assert hass.states.get(TRV_ID).state == HVACMode.OFF
    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF


async def test_a_released_cooler_runs_its_own_sequence(hass):
    """A cooler run by hand or by an automation is not switched back.

    The room heats and cools, and the room is warm enough that the cooler
    should run. Released, the cooler is switched off at its remote, set to a
    setpoint of its own and switched on again; none of it is undone or taken
    over. Taken back, the cooler gets the room's own cooling setpoint once.
    """
    _, cooler = await build_devices(hass, ROOM.trv, ROOM_AC_COOLER)
    set_room_sensor(hass, WARM_ROOM)
    entry = make_entry(ROOM)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await _call(hass, SERVICE_SET_HVAC_MODE, {"hvac_mode": HVACMode.HEAT_COOL})
    assert hass.states.get(COOLER_ID).state == HVACMode.COOL
    cool_target_temperature = bt.cool_target_temperature

    await _set_release(hass, bt, True)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    _at_the_device(cooler, hvac_mode=HVACMode.OFF)
    await _settle(hass)
    _at_the_device(cooler, setpoint=18.0)
    await _settle(hass)
    _at_the_device(cooler, hvac_mode=HVACMode.COOL)
    await _settle(hass)
    _at_the_device(cooler, hvac_mode=HVACMode.OFF)
    await _let_minutes_pass(hass, 15)

    assert _device_writes(events, bt) == []
    assert hass.states.get(COOLER_ID).state == HVACMode.OFF
    assert bt.cool_target_temperature == cool_target_temperature

    with patch(WRITE_BUDGET, 0.0):
        await _set_release(hass, bt, False)
        await _let_minutes_pass(hass, 15)

    assert mode_commands(events, COOLER_ID) == [HVACMode.COOL]
    assert hass.states.get(COOLER_ID).state == HVACMode.COOL


async def test_a_released_trv_going_away_raises_no_repair(hass, fake_trv):
    """An outage of a released device is the user's business.

    Once the devices are taken back, a TRV that is still gone is reported.
    """
    set_room_sensor(hass, 19.0)
    entry = make_entry(fake_trv.profile)
    with patch(CRITICAL_GRACE, timedelta(seconds=0)):
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        await _set_release(hass, bt, True)

        fake_trv.set_available(False)
        await _let_minutes_pass(hass, 5)
        issue = entry_issue_id(entry.entry_id, "missing_entity", TRV_ID)
        assert (DOMAIN, issue) not in ir.async_get(hass).issues
        assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT

        await _set_release(hass, bt, False)
        assert (DOMAIN, issue) in ir.async_get(hass).issues


@pytest.mark.parametrize(
    "fake_trv", [GENERIC_HEAT_TRV, MQTT_OFFSET_TRV], indirect=True, ids=profile_id
)
async def test_a_release_survives_a_restart_without_a_write(hass, fake_trv):
    """A thermostat that starts released sets up nothing on its devices.

    Setting a TRV up writes to it where the device has more than a setpoint,
    such as the offset a Zigbee2MQTT head is reset to. The switch comes back on, the
    thermostat shows its saved target, and a report from the TRV sets
    nothing up either. Taken back, the TRV is set up and controlled like at
    any start.
    """
    mock_restore_cache(
        hass,
        [
            State(
                BT_ENTITY,
                HVACMode.HEAT,
                {"temperature": 22.5, "devices_released": True},
            )
        ],
    )
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    hass.set_state(CoreState.starting)
    entry = make_entry(fake_trv.profile)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    bt = await wait_for_startup(hass, entry)
    # A report from a TRV startup left alone is otherwise what sets it up.
    _at_the_device(fake_trv, setpoint=18.0)
    await _let_minutes_pass(hass, 5)

    assert bt.devices_released is True
    assert hass.states.get(_release_switch(hass, bt)).state == STATE_ON
    assert hass.states.get(BT_ENTITY).attributes["temperature"] == 22.5
    assert _device_writes(events, bt) == []

    await _set_release(hass, bt, False)
    await _let_minutes_pass(hass, 5)

    assert hass.states.get(_release_switch(hass, bt)).state == STATE_OFF
    assert bt.real_trvs[TRV_ID].awaiting_initialization is False
    assert len(setpoint_commands(events, TRV_ID)) == 1


async def test_a_released_room_starts_while_its_trv_is_away(hass, fake_trv):
    """A room restarted with its devices released starts without them.

    The TRV is off the air for the whole start, as a head with its batteries
    taken out for the summer is. The room comes up on its saved mode and
    target and writes nothing. Taken back while the TRV is still away, the
    room has nothing to control; once the TRV reports again it is set up
    and controlled.
    """
    mock_restore_cache(
        hass,
        [
            State(
                BT_ENTITY,
                HVACMode.HEAT,
                {"temperature": 22.5, "devices_released": True},
            )
        ],
    )
    fake_trv.set_available(False)
    hass.states.async_set(SENSOR_ID, "19.5", {"unit_of_measurement": "°C"})
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    with patch(CRITICAL_GRACE, timedelta(seconds=0)):
        entry = make_entry(fake_trv.profile)
        await setup_entry(hass, entry)
        bt = await wait_for_startup(hass, entry)
        await _let_minutes_pass(hass, 5)

        published = hass.states.get(BT_ENTITY)
        assert published.state == HVACMode.HEAT
        assert published.attributes["temperature"] == 22.5
        assert published.attributes["devices_released"] is True
        assert _device_writes(events, bt) == []

        await _set_release(hass, bt, False)
        assert hass.states.get(BT_ENTITY).state == "unavailable"

        fake_trv.set_available(True)
        await _let_minutes_pass(hass, 5)

    assert bt.real_trvs[TRV_ID].awaiting_initialization is False
    assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT
    assert setpoint_commands(events, TRV_ID)
