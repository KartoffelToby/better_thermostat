"""A room with more than one head, driven end to end.

The rest of the suite drives one device per config entry, so everything it
proves is a statement about a single head. These are the questions that only
have an answer with several: whether one head speaking for itself can speak
for the room, whether the heads that are still there keep heating while one is
gone, whether two heads of different models each get what they can express,
and how one room-level valve command is split between them.
"""

from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    HVACMode,
)
from homeassistant.const import EVENT_CALL_SERVICE
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.better_thermostat.utils.calibration.mpc import (
    DISTRIBUTE_COMPENSATION_PCT_PER_K,
)

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    assert_profile_adopted,
    assert_write_is,
    make_entry,
    mode_commands,
    profile_id,
    set_room_sensor,
    setpoint_commands,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import (
    GROUP_OF_THREE,
    GROUP_SCENARIOS,
    MIXED_GRID_GROUP,
    VALVE_GROUP,
)


async def report_mode(hass, heads, mode: HVACMode) -> None:
    """Let every head in ``heads`` publish ``mode``, as if a dial was turned.

    Driven through the real climate service rather than by writing the state,
    so the device confirms the mode the way it confirms one Better Thermostat
    sends — the state change Better Thermostat then sees is the same either
    way round, which is exactly what makes it worth telling apart.

    Several heads go in one call because Better Thermostat writes back at
    them: a room turned off head by head has its first head commanded back
    into heat before the last one has been touched, so no instant exists at
    which the room ever was off.
    """
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {"entity_id": [head.entity_id for head in heads], "hvac_mode": mode},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("trv_group", GROUP_SCENARIOS, indirect=True, ids=profile_id)
async def test_startup_adopts_every_head_of_the_group(hass, trv_group):
    """Every head in the entry is read, with its own capabilities.

    The guard under the rest of this file: a group whose second head never
    made it into ``real_trvs`` still passes everything that only looks at the
    first, and a group whose heads were all read as one shape no longer tells
    them apart at all.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    assert list(bt.real_trvs) == [p.entity_id for p in trv_group.scenario.profiles]
    for profile in trv_group.scenario.profiles:
        assert_profile_adopted(bt, profile)


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_one_head_reporting_off_does_not_switch_the_room_off(hass, trv_group):
    """A single valve dropping out of heat leaves the room heating.

    A head enters frost protection, or somebody turns one dial down, and Home
    Assistant reports that as ``off``. Adopting it as the room's mode is what
    made a whole flat go cold from one valve (#2063): the other heads are
    still asking for heat, and nothing but this head said otherwise.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert bt.bt_hvac_mode == HVACMode.HEAT

    await report_mode(hass, [trv_group[1]], HVACMode.OFF)

    assert bt.bt_hvac_mode == HVACMode.HEAT
    assert hass.states.get(BT_ENTITY).state == HVACMode.HEAT


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_the_room_switches_off_once_every_head_reports_off(hass, trv_group):
    """A room whose heads all went off follows them.

    The other half of the rule, and the reason the one above is a rule and not
    a refusal: the mode is still adopted from the devices, just not from one
    of them. Without this a quorum that never passes reads exactly like a
    quorum that works.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    await report_mode(hass, trv_group.entities, HVACMode.OFF)

    assert await wait_for(hass, lambda: bt.bt_hvac_mode == HVACMode.OFF)
    assert hass.states.get(BT_ENTITY).state == HVACMode.OFF


@pytest.mark.parametrize("trv_group", [GROUP_OF_THREE], indirect=True, ids=profile_id)
async def test_the_group_keeps_heating_the_room_while_one_head_is_gone(hass, trv_group):
    """A head that drops off the air takes only itself out of the room.

    The bulkhead: a battery head out of radio range must not stop the heads
    that are still reachable from being commanded, because the room is still
    cold and they can still heat it. The absent head is left alone rather than
    written into the void — coming back is what the reachability backoff
    watches for, and a command sent meanwhile would be lost anyway.
    """
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)

    absent = trv_group[1]
    present = [head for head in trv_group.entities if head is not absent]
    absent.set_available(False)
    await hass.async_block_till_done()
    baselines = {head.entity_id: len(head.set_temperature_calls) for head in present}
    # Only from here on does the absence mean anything, so the bus is read
    # from here on too: everything before it was addressed to a head that
    # was still there.
    events = async_capture_events(hass, EVENT_CALL_SERVICE)

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 23.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: all(
                len(head.set_temperature_calls) > baselines[head.entity_id]
                for head in present
            ),
        ), {head.entity_id: head.set_temperature_calls for head in present}

    for head in present:
        assert_write_is(head.set_temperature_calls[-1], 23.0, head.profile)
    assert setpoint_commands(events, absent.entity_id) == []
    assert bt.bt_target_temp == pytest.approx(23.0)


@pytest.mark.parametrize("trv_group", [MIXED_GRID_GROUP], indirect=True, ids=profile_id)
async def test_two_heads_of_different_models_each_get_what_they_can_express(
    hass, trv_group
):
    """One room target reaches two heads in the two shapes they accept.

    A room is rarely fitted out in one go, so its heads differ in the grid
    their setpoint sits on and in what they call the mode they heat in. The
    room holds one target either way, and rounding it once for the room would
    put it beside the grid of whichever head did not get to decide.
    """
    half_degree, whole_degree = trv_group.scenario.profiles
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    events = async_capture_events(hass, EVENT_CALL_SERVICE)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, half_degree)
    assert_profile_adopted(bt, whole_degree)
    # Startup writes a setpoint of its own, so the write under test is the
    # last one past this mark rather than the only one.
    baselines = {
        head.entity_id: len(head.set_temperature_calls) for head in trv_group.entities
    }

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 21.5},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: all(
                len(head.set_temperature_calls) > baselines[head.entity_id]
                for head in trv_group.entities
            ),
        ), {head.entity_id: head.set_temperature_calls for head in trv_group.entities}

    for head in trv_group.entities:
        assert_write_is(head.set_temperature_calls[-1], 21.5, head.profile)
    assert mode_commands(events, whole_degree.entity_id) == []
    assert hass.states.get(whole_degree.entity_id).state == HVACMode.HEAT_COOL


@pytest.mark.parametrize("trv_group", [VALVE_GROUP], indirect=True, ids=profile_id)
async def test_the_colder_head_of_a_valve_group_is_opened_further(hass, trv_group):
    """One room-level valve command reaches the heads as two openings.

    The heads of a room do not sit in the same air: one is by the window and
    reads colder than the other. The controller computes a single opening for
    the room, and splitting it means the colder head opens further while the
    warmest gets the room's figure unchanged — which is the only thing that
    makes the difference between the two openings mean anything.

    The target is barely above the room, because the split is an addition on
    top of the room's opening: a room that calls for everything the heads have
    puts both of them at fully open, where no difference can show.
    """
    warm_head, cold_head = trv_group.entities
    set_room_sensor(hass, 19.5)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    warm_valve, cold_valve = warm_head.valve_number, cold_head.valve_number
    expected_extra = DISTRIBUTE_COMPENSATION_PCT_PER_K * (
        warm_head.profile.current_temperature - cold_head.profile.current_temperature
    )

    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            CLIMATE_DOMAIN,
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 20.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: (
                any(v > 0 for v in warm_valve.set_value_calls)
                and any(v > 0 for v in cold_valve.set_value_calls)
            ),
        ), (warm_valve.set_value_calls, cold_valve.set_value_calls)

    opened_warm = warm_valve.set_value_calls[-1]
    opened_cold = cold_valve.set_value_calls[-1]
    # Both ends of the split have to be off their stops for the difference to
    # be the compensation rather than a clamp.
    assert 0.0 < opened_warm < 100.0
    assert opened_cold < 100.0
    assert opened_cold - opened_warm == pytest.approx(expected_extra, abs=1.0)
    assert bt.real_trvs[cold_head.entity_id].last_valve_percent > (
        bt.real_trvs[warm_head.entity_id].last_valve_percent
    )
