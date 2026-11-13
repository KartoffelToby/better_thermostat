"""A head that drops an unrefreshed valve demand keeps the one it was given.

A Bosch BTH-RA under Zigbee2MQTT holds a ``pi_heating_demand`` written to it
for a quarter of an hour and then returns to its own controller, which shuts
the valve; the next control cycle opens it again within seconds. The opening
it holds is written again before that window ends, a closed valve included
while Better Thermostat keeps the head heating, but not while it holds the
head off. A head without that behaviour is written only when its opening
changes.
"""

from datetime import timedelta
import importlib
from itertools import pairwise
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.core.clock import FakeClock

from .conftest import (
    BT_ENTITY,
    WRITE_BUDGET,
    assert_profile_adopted,
    make_entry,
    profile_id,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)
from .device_profiles import BOSCH_BTH_RA_VALVE_TRV, VALVE_TRV

# The silence after which a BTH-RA returns to its own controller.
FALLBACK_SECONDS = 15 * 60
KEEPALIVE_SECONDS = importlib.import_module(
    "custom_components.better_thermostat.model_fixes.BTH-RA"
).VALVE_KEEPALIVE_INTERVAL_SECONDS
STEP_SECONDS = 30


async def _open_the_valve(hass, fake_trv):
    """Start the entry and have the room call for heat; return the thermostat."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    assert_profile_adopted(bt, fake_trv.profile)
    # The periodic ticks are registered at the very end of startup.
    await hass.async_block_till_done()

    valve_number = fake_trv.valve_number
    with patch(WRITE_BUDGET, 0.0):
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": BT_ENTITY, "temperature": 22.0},
            blocking=True,
        )
        assert await wait_for(
            hass,
            lambda: (
                valve_number.set_value_calls and valve_number.set_value_calls[-1] > 0
            ),
        )
    await hass.async_block_till_done()
    return bt


async def _close_the_valve(hass, fake_trv, change):
    """Open the valve, then apply ``change`` until a close is written; return bt.

    ``change`` takes ``hass`` and is awaited with the write budget lifted, so
    the close it causes goes out at once.
    """
    bt = await _open_the_valve(hass, fake_trv)
    valve_number = fake_trv.valve_number
    with patch(WRITE_BUDGET, 0.0):
        await change(hass)
        assert await wait_for(
            hass,
            lambda: (
                valve_number.set_value_calls and valve_number.set_value_calls[-1] == 0
            ),
        )
    await hass.async_block_till_done()
    return bt


async def _warm_the_room(hass):
    """Put the room above its target, so the heating power closes the valve."""
    set_room_sensor(hass, 24.0)
    await hass.async_block_till_done()


async def _turn_the_thermostat_off(hass):
    """Turn Better Thermostat off, which turns its heads off."""
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {"entity_id": BT_ENTITY, "hvac_mode": HVACMode.OFF},
        blocking=True,
    )


def _assert_kept_alive(writes, value):
    """Every write repeats ``value`` and no fallback window passes without one."""
    assert [written for _, written in writes] == [value] * len(writes)
    times = [0] + [at for at, _ in writes]
    gaps = [later - earlier for earlier, later in pairwise(times)]
    assert len(writes) >= 2, writes
    assert all(gap < FALLBACK_SECONDS for gap in gaps), gaps
    # The keepalive refreshes a held demand; it does not hammer the head.
    assert all(gap >= KEEPALIVE_SECONDS for gap in gaps[1:]), gaps


async def _writes_over(hass, bt, fake_trv, seconds):
    """Let ``seconds`` pass on the clock and the timers; return each write's time.

    Every write is reported with the seconds that had passed when it was
    seen and the value it put on the valve number.
    """
    clock = FakeClock(monotonic_value=bt.clock.monotonic())
    bt.clock = clock
    start = dt_util.utcnow()
    calls = fake_trv.valve_number.set_value_calls
    seen = len(calls)
    writes: list[tuple[int, float]] = []
    elapsed = 0
    while elapsed < seconds:
        clock.advance(STEP_SECONDS)
        elapsed += STEP_SECONDS
        async_fire_time_changed(hass, start + timedelta(seconds=elapsed))
        await hass.async_block_till_done()
        writes.extend((elapsed, value) for value in calls[seen:])
        seen = len(calls)
    return writes


@pytest.mark.parametrize(
    "fake_trv", [BOSCH_BTH_RA_VALVE_TRV], indirect=True, ids=profile_id
)
async def test_a_bth_ra_is_written_its_opening_before_it_falls_back(hass, fake_trv):
    """The held opening reaches the head again inside every fallback window.

    Two windows long, so the second write proves the interval restarts at
    the keepalive rather than at the opening's first write.
    """
    bt = await _open_the_valve(hass, fake_trv)
    opened = fake_trv.valve_number.set_value_calls[-1]

    writes = await _writes_over(hass, bt, fake_trv, 2 * FALLBACK_SECONDS)

    _assert_kept_alive(writes, opened)


@pytest.mark.parametrize(
    "fake_trv", [BOSCH_BTH_RA_VALVE_TRV], indirect=True, ids=profile_id
)
async def test_a_bth_ra_held_shut_in_heat_mode_is_written_its_close_again(
    hass, fake_trv
):
    """A warm room's closed valve reaches the head again inside every window.

    The head keeps heating, so its own controller would open the valve
    once the demand Better Thermostat wrote lapses.
    """
    bt = await _close_the_valve(hass, fake_trv, _warm_the_room)

    writes = await _writes_over(hass, bt, fake_trv, 2 * FALLBACK_SECONDS)

    assert hass.states.get(fake_trv.profile.entity_id).state == HVACMode.HEAT
    _assert_kept_alive(writes, 0)


@pytest.mark.parametrize(
    "fake_trv", [BOSCH_BTH_RA_VALVE_TRV], indirect=True, ids=profile_id
)
async def test_a_bth_ra_turned_off_is_not_written_its_close_again(hass, fake_trv):
    """A head Better Thermostat holds off takes no write while it stays off."""
    bt = await _close_the_valve(hass, fake_trv, _turn_the_thermostat_off)

    writes = await _writes_over(hass, bt, fake_trv, 2 * FALLBACK_SECONDS)

    assert hass.states.get(fake_trv.profile.entity_id).state == HVACMode.OFF
    assert writes == []


@pytest.mark.parametrize("fake_trv", [VALVE_TRV], indirect=True, ids=profile_id)
async def test_a_head_that_holds_its_demand_is_written_only_on_a_change(hass, fake_trv):
    """Without the quirk an opening the head holds is not written again."""
    bt = await _open_the_valve(hass, fake_trv)

    assert await _writes_over(hass, bt, fake_trv, 2 * FALLBACK_SECONDS) == []
