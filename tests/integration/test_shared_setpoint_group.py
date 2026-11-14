"""A room whose heads the system they belong to keeps on one setpoint."""

from dataclasses import replace
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
import pytest

from custom_components.better_thermostat.utils.scheduler import request_control_cycle

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import SHARED_SETPOINT_GROUP, GroupScenario

# The follower reads warmer than the leader, so the calibration writes the
# two heads different setpoints and the copy replaces the follower's own.
WARMER_FOLLOWER_GROUP = GroupScenario(
    name="warmer_follower",
    profiles=(
        replace(SHARED_SETPOINT_GROUP.profiles[0], current_temperature=21.0),
        SHARED_SETPOINT_GROUP.profiles[1],
    ),
)


@pytest.fixture(autouse=True)
def immediate_writes():
    """Exercise the device writes without waiting for ordinary write spacing."""
    with patch(WRITE_BUDGET, 0.0):
        yield


@pytest.mark.parametrize(
    "trv_group", [WARMER_FOLLOWER_GROUP], indirect=True, ids=profile_id
)
async def test_a_setpoint_copied_from_the_other_head_leaves_the_room_target(
    hass, trv_group
):
    """The follower reporting the leader's setpoint has not been turned.

    The calibration writes each head a setpoint of its own. The leader's
    write reaches the follower as well, and the follower reports it. That is
    Better Thermostat's own write to the leader, so the room target stays
    where the user set it, and the next control cycle writes the follower a
    setpoint of its own again.
    """
    follower, leader = trv_group[0], trv_group[1]
    set_room_sensor(hass, 19.0)
    entry = make_entry(trv_group.scenario)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    copies_before = len(follower.copied_setpoints)

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": 21.0},
        blocking=True,
    )
    assert await wait_for(
        hass,
        lambda: any(
            copied != follower.set_temperature_calls[-1]
            for copied in follower.copied_setpoints[copies_before:]
        ),
    )
    await hass.async_block_till_done()
    assert leader.set_temperature_calls
    assert bt.heat_target_temperature == 21.0

    writes_before = len(follower.set_temperature_calls)
    request_control_cycle(bt)
    assert await wait_for(
        hass, lambda: len(follower.set_temperature_calls) > writes_before
    )
    await hass.async_block_till_done()
    assert bt.heat_target_temperature == 21.0
