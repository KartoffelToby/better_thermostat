"""A setpoint the TRV does not take is written again, paced, and reported.

A write lost on the air is repeated by the next control cycle and the device
converges without anyone noticing. A device that keeps holding another value
is written at a growing distance instead of on every cycle, and once a run of
writes has gone unanswered the TRV is listed in the thermostat's errors and
carries a repair issue. Both go away as soon as the device holds the value.
"""

import asyncio
from datetime import timedelta
import json
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.better_thermostat.utils.controlling import (
    MIN_WRITE_INTERVAL_S,
    SETPOINT_RETRY_BACKOFF_MAX_S,
    SETPOINT_UNAPPLIED_WRITES_LIMIT,
)
from custom_components.better_thermostat.utils.helpers import entry_issue_id

from .conftest import (
    BT_ENTITY,
    DOMAIN,
    WRITE_BUDGET,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)

# Below the room reading, so the head is asked for a setpoint it does not
# hold yet: a room below its target already has the head on its maximum.
_NEW_TARGET = 16.0
# Reconcile ticks a test may fire before it gives up on a write arriving.
_MAX_TICKS = 40


class _VirtualTime:
    """A clock that advances by every pause the integration asks for.

    The pauses return at once, so the hours a backed-off run of writes spans
    pass in a moment, and the write budget reads the same clock.
    """

    def __init__(self, real_clock) -> None:
        self.t = 0.0
        self._real = real_clock
        self._base = real_clock.monotonic()

    def monotonic(self) -> float:
        return self._base + self.t

    def now(self):
        return self._real.now() + timedelta(seconds=self.t)

    def utcnow(self):
        return self._real.utcnow() + timedelta(seconds=self.t)


class _Head:
    """What the simulated head does with the setpoints written to it."""

    def __init__(self, device, clock: _VirtualTime) -> None:
        self.device = device
        self.clock = clock
        self.takes_writes = False
        self.writes: list[tuple[float, float]] = []

    async def receive(self, **kwargs) -> None:
        """Record a setpoint write and apply it only while the head takes them."""
        temperature = kwargs["temperature"]
        assert isinstance(temperature, float)
        self.writes.append((self.clock.t, temperature))
        self.device.set_temperature_calls.append(temperature)
        if self.takes_writes:
            self.device._attr_target_temperature = temperature
            self.device.async_write_ha_state()


class _ReconcileTicks:
    """Fire the five-minute reconcile tick, one tick further each time."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self.start = dt_util.utcnow()
        self.count = 0

    async def fire(self) -> None:
        self.count += 1
        async_fire_time_changed(
            self.hass, self.start + timedelta(minutes=6 * self.count)
        )
        await self.hass.async_block_till_done()


def _errors(hass) -> list[str]:
    return json.loads(hass.states.get(BT_ENTITY).attributes["errors"])


def _issue(hass, entry, entity_id: str):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, entry_issue_id(entry.entry_id, "setpoint_not_applied", entity_id)
    )


async def _set_target(hass, temperature: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": temperature},
        blocking=True,
    )


@pytest.fixture
async def stubborn_room(hass, fake_trv):
    """A running room whose head ignores every setpoint until told otherwise."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    clock = _VirtualTime(bt.clock)
    head = _Head(fake_trv, clock)
    real_sleep = asyncio.sleep

    async def virtual_sleep(delay, result=None, **kwargs):
        if delay and delay > 0:
            clock.t += delay
        await real_sleep(0.001 if delay and delay > 0 else 0)
        return result

    async def receive(device, **kwargs):
        await head.receive(**kwargs)

    with (
        patch("asyncio.sleep", new=virtual_sleep),
        patch.object(type(fake_trv), "async_set_temperature", receive),
    ):
        bt.clock = clock
        yield bt, entry, head


async def _write_until(hass, head: _Head, ticks: _ReconcileTicks, count: int) -> None:
    """Fire reconcile ticks until the head has received ``count`` writes."""
    while len(head.writes) < count and ticks.count < _MAX_TICKS:
        before = len(head.writes)
        await ticks.fire()
        await wait_for(hass, lambda: len(head.writes) > before, timeout_seconds=2.0)
    assert len(head.writes) >= count, head.writes


async def test_a_lost_write_is_sent_again_and_the_head_converges(hass, fake_trv):
    """A write the radio lost reaches the head on the next attempt, unreported."""
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    trv = bt.real_trvs[fake_trv.entity_id]
    ticks = _ReconcileTicks(hass)

    with patch(WRITE_BUDGET, 0.0):
        fake_trv.drop_next_setpoint_write = True
        baseline = len(fake_trv.set_temperature_calls)
        await _set_target(hass, _NEW_TARGET)
        assert await wait_for(
            hass, lambda: len(fake_trv.set_temperature_calls) > baseline
        )
        lost = fake_trv.set_temperature_calls[-1]
        assert fake_trv.target_temperature != lost

        await ticks.fire()
        assert await wait_for(hass, lambda: fake_trv.target_temperature == lost)

    assert fake_trv.set_temperature_calls[baseline:] == [lost, lost]
    assert await wait_for(hass, lambda: trv.unapplied_setpoint_writes == 0)
    assert _errors(hass) == []
    assert _issue(hass, entry, fake_trv.entity_id) is None


async def test_a_head_that_ignores_the_setpoint_is_written_at_a_growing_distance(
    hass, stubborn_room
):
    """Each further write waits twice as long as the one before, up to an hour."""
    _bt, _entry, head = stubborn_room
    ticks = _ReconcileTicks(hass)

    await _set_target(hass, _NEW_TARGET)
    await _write_until(hass, head, ticks, 10)

    times = [t for t, _ in head.writes[:10]]
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    expected = [
        min(MIN_WRITE_INTERVAL_S * 2**i, SETPOINT_RETRY_BACKOFF_MAX_S)
        for i in range(len(gaps))
    ]
    assert all(gap >= wait for gap, wait in zip(gaps, expected, strict=True)), gaps
    assert expected[-1] == SETPOINT_RETRY_BACKOFF_MAX_S


async def test_a_head_that_ignores_the_setpoint_is_reported_until_it_takes_it(
    hass, stubborn_room
):
    """The TRV is listed in the errors with a repair issue, and leaves both again.

    Nothing is reported while the run is shorter than its limit. Once the head
    takes the setpoint, the report is taken back without anyone acting on it.
    """
    bt, entry, head = stubborn_room
    entity_id = head.device.entity_id
    trv = bt.real_trvs[entity_id]
    ticks = _ReconcileTicks(hass)

    await _set_target(hass, _NEW_TARGET)
    await _write_until(hass, head, ticks, SETPOINT_UNAPPLIED_WRITES_LIMIT)
    assert _errors(hass) == []
    assert _issue(hass, entry, entity_id) is None

    while not trv.setpoint_unapplied_reported and ticks.count < _MAX_TICKS:
        await ticks.fire()
    assert trv.setpoint_unapplied_reported
    assert _errors(hass) == [entity_id]
    issue = _issue(hass, entry, entity_id)
    assert issue is not None
    assert issue.translation_key == "setpoint_not_applied"
    assert issue.translation_placeholders["entity"] == entity_id
    assert issue.learn_more_url == (
        "https://better-thermostat.org/faq/setpoint-not-applied"
    )

    head.takes_writes = True
    writes = len(head.writes)
    await _write_until(hass, head, ticks, writes + 1)
    assert head.device.target_temperature == head.writes[-1][1]

    assert await wait_for(hass, lambda: not trv.setpoint_unapplied_reported)
    assert _errors(hass) == []
    assert _issue(hass, entry, entity_id) is None


async def test_a_user_change_reaches_a_backed_off_head_at_the_normal_pace(
    hass, stubborn_room
):
    """A new target from the user does not wait out the run's backoff.

    The run is long enough for the next write to wait an hour. The user's
    target goes out once the normal interval after the last write has passed;
    the head's own reports in between may stretch that, but by minutes, not
    by the hour the run would wait.
    """
    _bt, _entry, head = stubborn_room
    ticks = _ReconcileTicks(hass)

    await _set_target(hass, _NEW_TARGET)
    await _write_until(hass, head, ticks, SETPOINT_UNAPPLIED_WRITES_LIMIT + 2)

    writes = len(head.writes)
    await _set_target(hass, _NEW_TARGET - 1.0)
    assert await wait_for(hass, lambda: len(head.writes) > writes)
    gap = head.writes[writes][0] - head.writes[writes - 1][0]
    assert gap < SETPOINT_RETRY_BACKOFF_MAX_S / 2, gap
