"""A head that holds less than Better Thermostat writes keeps the room's target.

A head can publish a maximum it does not hold: the calibrated setpoint goes out
above the head's real ceiling, the head holds the ceiling, and the setpoint
watchdog never sees the value it waits for. What the head reports after the
watchdog gave up is that ceiling, which is Better Thermostat's own write as the
head took it, not a turn at the knob.
"""

from unittest.mock import patch

from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_TEMPERATURE,
)
from homeassistant.core import Context
import pytest

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    SimulatedClimate,
    build_devices,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import TRV_ID, ZWAVE_CAPPED_TRV
from .write_hold import CONFIRM_TIMEOUT

ROOM_TEMPERATURE = 21.5
ROOM_TARGET = 20.5
# The head reads far above the room, so the calibration asks it for a
# setpoint above the ceiling it holds.
HEAD_TEMPERATURE = 30.0
# Short enough for the watchdog to give up within the test, long enough for
# the head to have answered the write before it does.
SHORT_CONFIRM_TIMEOUT_S = 3
CEILING = 28.0
assert ZWAVE_CAPPED_TRV.setpoint_ceiling == CEILING


def _above_ceiling(call: float | dict[str, float]) -> bool:
    """Return whether a recorded setpoint write went out above the ceiling."""
    return isinstance(call, float) and call > CEILING


def _publish(device: SimulatedClimate) -> None:
    """Publish the device's state as a report of its own."""
    device.async_set_context(Context())
    device.async_write_ha_state()


async def _capped_head(hass):
    """Return the entity and the head once the watchdog gave up on a capped write."""
    (head,) = await build_devices(hass, ZWAVE_CAPPED_TRV)
    head._attr_current_temperature = HEAD_TEMPERATURE
    _publish(head)
    set_room_sensor(hass, ROOM_TEMPERATURE)
    entry = make_entry(ZWAVE_CAPPED_TRV)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[TRV_ID]

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {"entity_id": BT_ENTITY, "temperature": ROOM_TARGET},
        blocking=True,
    )
    assert await wait_for(
        hass, lambda: any(_above_ceiling(call) for call in head.set_temperature_calls)
    ), f"no write above the ceiling: {head.set_temperature_calls}"
    assert head.target_temperature == CEILING
    assert await wait_for(
        hass, lambda: trv.target_temperature_received and not bt.ignore_states
    )
    assert bt.heat_target_temperature == ROOM_TARGET
    return bt, head


@pytest.fixture
def _short_confirm_timeout():
    """Let the setpoint watchdog give up within the test."""
    with patch(WRITE_BUDGET, 0.0), patch(CONFIRM_TIMEOUT, SHORT_CONFIRM_TIMEOUT_S):
        yield


@pytest.mark.usefixtures("_short_confirm_timeout")
async def test_the_ceiling_a_head_holds_is_not_read_as_a_turn(hass):
    """A report that carries the head's ceiling leaves the room target alone.

    The watchdog gave up on the calibrated write while the head held its
    ceiling. The head's next report moves only its own reading and carries
    the ceiling again; the room keeps the target the user set.
    """
    bt, head = await _capped_head(hass)

    head._attr_current_temperature = HEAD_TEMPERATURE + 0.2
    _publish(head)
    await hass.async_block_till_done()

    assert (
        await wait_for(hass, lambda: bt.heat_target_temperature != ROOM_TARGET, 1.0)
        is False
    ), f"the room took {bt.heat_target_temperature}"


@pytest.mark.usefixtures("_short_confirm_timeout")
async def test_a_turn_away_from_the_ceiling_is_the_users_target(hass):
    """A knob turned at a head that holds its ceiling becomes the room's target."""
    bt, head = await _capped_head(hass)

    head._attr_target_temperature = 23.0
    _publish(head)

    assert await wait_for(hass, lambda: bt.heat_target_temperature == 23.0), (
        f"the room kept {bt.heat_target_temperature}"
    )
