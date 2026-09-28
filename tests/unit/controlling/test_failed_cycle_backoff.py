"""A control cycle that keeps failing is retried at a growing distance.

A device that refuses a payload for good (a setpoint outside its range, a mode
it does not offer) fails every cycle that sends it. The control queue retries
such a cycle on its own, and these tests pin how far apart those retries land,
when a run of failures ends, what the log says about it, and that a request
arriving in the meantime is not held back by the pause.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.climate.const import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import (
    FAILED_CYCLE_BACKOFF_MAX_S,
    FAILED_CYCLE_BACKOFF_S,
    MIN_WRITE_INTERVAL_S,
    control_queue,
)

_CTRL = "custom_components.better_thermostat.utils.controlling"
_TRV = "climate.trv1"

_REAL_SLEEP = asyncio.sleep


class _VirtualSleep:
    """Stand-in for ``asyncio.sleep`` that records every pause it is asked for.

    A pause returns at once unless ``hold`` is set, in which case a pause of a
    second or more waits until ``release`` is called. Holding lets a test act
    while the queue's retry is still pending.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.hold = False
        self._released = asyncio.Event()

    def release(self) -> None:
        self.hold = False
        self._released.set()

    async def __call__(self, delay, result=None):
        if delay and delay > 0:
            self.waits.append(delay)
            if self.hold and delay >= 1:
                await self._released.wait()
        await _REAL_SLEEP(0)
        return result


def _make_self() -> Mock:
    entity = Mock()
    entity.device_name = "room"
    entity.in_maintenance = False
    entity.ignore_states = False
    entity.startup_running = False
    entity.calculate_heating_power = AsyncMock()
    entity.calculate_heat_loss = AsyncMock()
    entity.cooler_entity_id = None
    entity.real_trvs = {_TRV: Trv.from_legacy_dict(_TRV, {})}
    entity.bt_target_temp = 21.0
    entity.bt_target_cooltemp = None
    entity.bt_hvac_mode = HVACMode.HEAT
    entity.clock = FakeClock()
    entity.hass.states.get.return_value = None
    entity.control_queue_task = asyncio.Queue(maxsize=1)
    return entity


def _refused(*_args, **_kwargs):
    raise ServiceValidationError("out of range")


class _Queue:
    """Run the control queue of one entity against a scripted control_trv."""

    def __init__(self, entity: Mock, outcomes) -> None:
        self.entity = entity
        self.outcomes = outcomes
        self.calls = 0
        self.sleep = _VirtualSleep()
        self._task: asyncio.Task | None = None
        self._patches = []

    async def _control_trv(self, _entity, _entity_id, cycle=None):
        outcome = self.outcomes(self.calls)
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def __aenter__(self):
        self._patches = [
            patch(f"{_CTRL}.control_trv", new=self._control_trv),
            patch(f"{_CTRL}.compute_control_cycle", return_value=None),
            patch("asyncio.sleep", new=self.sleep),
        ]
        for p in self._patches:
            p.start()
        self._task = asyncio.create_task(control_queue(self.entity))
        self.entity.control_queue_task.put_nowait(self.entity)
        return self

    async def __aexit__(self, *exc):
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        for p in reversed(self._patches):
            p.stop()

    async def until_calls(self, count: int, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.calls < count:
            if loop.time() >= deadline:
                raise AssertionError(
                    f"control_trv ran {self.calls} times, expected {count}"
                )
            await _REAL_SLEEP(0)
        # Let the round that just ran reach its bookkeeping.
        for _ in range(5):
            await _REAL_SLEEP(0)

    def request(self) -> None:
        self.entity.control_queue_task.put_nowait(self.entity)


@pytest.mark.asyncio
async def test_a_cycle_that_keeps_failing_the_same_way_waits_longer_each_time():
    """The pause doubles with every failure of the same cycle, up to its cap."""
    async with _Queue(
        _make_self(), lambda _n: ServiceValidationError("out of range")
    ) as queue:
        await queue.until_calls(12)

    expected = [FAILED_CYCLE_BACKOFF_S]
    while len(expected) < 11:
        expected.append(min(expected[-1] * 2, FAILED_CYCLE_BACKOFF_MAX_S))
    assert queue.sleep.waits[:11] == expected
    assert expected[-1] == FAILED_CYCLE_BACKOFF_MAX_S == 300.0


@pytest.mark.asyncio
async def test_a_trv_that_answers_false_is_paced_like_one_that_raises():
    """A worker that reports failure without raising escalates the same way."""
    async with _Queue(_make_self(), lambda _n: False) as queue:
        await queue.until_calls(5)

    assert queue.sleep.waits[:4] == [2.0, 4.0, 8.0, 16.0]


@pytest.mark.asyncio
async def test_a_retry_that_succeeds_ends_the_run():
    """After the retried cycle goes through, the next failure starts over."""
    script = {0: False, 1: False, 2: False, 3: True}
    async with _Queue(_make_self(), lambda n: script.get(n, False)) as queue:
        await queue.until_calls(4)
        queue.request()
        await queue.until_calls(5)

    assert queue.sleep.waits[:4] == [2.0, 4.0, 8.0, FAILED_CYCLE_BACKOFF_S]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("bt_target_temp", 23.5),
        ("bt_target_cooltemp", 26.0),
        ("bt_hvac_mode", HVACMode.OFF),
    ],
)
async def test_a_new_user_input_starts_a_new_run(attribute, value):
    """A failure after the user changed the room's target starts at the base."""
    entity = _make_self()
    async with _Queue(entity, lambda _n: ServiceValidationError("x")) as queue:
        await queue.until_calls(4)
        assert queue.sleep.waits[:3] == [2.0, 4.0, 8.0]
        waits_before = len(queue.sleep.waits)
        setattr(entity, attribute, value)
        queue.request()
        await queue.until_calls(queue.calls + 1)

    assert queue.sleep.waits[waits_before] == FAILED_CYCLE_BACKOFF_S


@pytest.mark.asyncio
async def test_a_payload_that_drifts_between_failures_keeps_the_run():
    """A write that moves with the room while the device refuses it is one run.

    The calibration moves the setpoint a worker sends from one cycle to the
    next, so the refused payload drifts without anyone setting a new target.
    """
    entity = _make_self()
    trv = entity.real_trvs[_TRV]

    def outcome(n):
        # The worker records what it sends before the device refuses it.
        trv.last_temperature = 22.0 + 0.1 * n
        return ServiceValidationError("out of range")

    async with _Queue(entity, outcome) as queue:
        await queue.until_calls(5)

    assert queue.sleep.waits[:4] == [2.0, 4.0, 8.0, 16.0]


@pytest.mark.asyncio
async def test_a_refusal_whose_message_varies_keeps_the_run(caplog):
    """A refusal that numbers its messages is paced and reported as one run."""
    entity = _make_self()
    caplog.set_level(logging.DEBUG, logger=_CTRL)
    async with _Queue(
        entity, lambda n: HomeAssistantError(f"timeout (tsn {n})")
    ) as queue:
        await queue.until_calls(5)

    assert queue.sleep.waits[:4] == [2.0, 4.0, 8.0, 16.0]
    tracebacks = [r for r in caplog.records if r.name == _CTRL and r.exc_info]
    assert len(tracebacks) == 1


@pytest.mark.asyncio
async def test_two_trvs_failing_in_turn_are_one_run(caplog):
    """Devices that fail alternately are paced as one run, each reported once."""
    other = "climate.trv2"
    entity = _make_self()
    entity.real_trvs[other] = Trv.from_legacy_dict(other, {})
    caplog.set_level(logging.DEBUG, logger=_CTRL)
    rounds = []

    async def control_trv(_entity, entity_id, cycle=None):
        rounds.append(entity_id)
        failing = _TRV if (len(rounds) - 1) // 2 % 2 == 0 else other
        if entity_id == failing:
            raise HomeAssistantError(f"no answer from {entity_id}")
        return True

    queue = _Queue(entity, None)
    queue._control_trv = control_trv
    async with queue:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while len(rounds) < 12 and loop.time() < deadline:
            await _REAL_SLEEP(0)

    assert queue.sleep.waits[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
    tracebacks = [r for r in caplog.records if r.name == _CTRL and r.exc_info]
    assert sorted(_TRV in r.getMessage() for r in tracebacks) == [False, True]


@pytest.mark.asyncio
async def test_a_clean_cycle_before_the_retry_is_due_keeps_the_run():
    """A cycle that wrote nothing while the retry is pending does not end the run.

    Such a cycle, for instance one the write budget deferred, never tried the
    refused write again, so it says nothing about whether the device accepts
    it now.
    """
    script = {0: _refused, 1: True}
    entity = _make_self()

    def outcome(n):
        step = script.get(n, _refused)
        return ServiceValidationError("out of range") if step is _refused else step

    async with _Queue(entity, outcome) as queue:
        queue.sleep.hold = True
        await queue.until_calls(1)
        queue.request()
        await queue.until_calls(2)
        queue.sleep.release()
        await queue.until_calls(3)

    assert queue.sleep.waits[:2] == [2.0, 4.0]


@pytest.mark.asyncio
async def test_a_request_during_the_pause_is_served_at_once():
    """A request that arrives during the pause gets its cycle straight away.

    A user who changes the target while a run of failures is pausing is not
    made to wait for the retry, and the entity listens to its devices again
    during the pause.
    """
    entity = _make_self()
    async with _Queue(entity, lambda _n: ServiceValidationError("x")) as queue:
        queue.sleep.hold = True
        await queue.until_calls(1)
        assert entity.ignore_states is False
        entity.bt_target_temp = 23.5
        queue.request()
        await queue.until_calls(2, timeout=1.0)


@pytest.mark.asyncio
async def test_the_first_retry_waits_for_the_setpoint_budget():
    """A retry is not due before the refused setpoint could be written again.

    The write budget stamps the failed write, so a retry inside that window
    writes nothing and would read as the device accepting the command.
    """
    entity = _make_self()
    entity.real_trvs[_TRV].last_write_monotonic = entity.clock.monotonic() - 5.0
    async with _Queue(entity, lambda _n: ServiceValidationError("x")) as queue:
        await queue.until_calls(1)

    assert queue.sleep.waits[0] == pytest.approx(MIN_WRITE_INTERVAL_S - 5.0)


@pytest.mark.asyncio
async def test_the_traceback_is_logged_once_per_run(caplog):
    """The first failure of a run carries the traceback, later ones one line."""
    entity = _make_self()
    caplog.set_level(logging.DEBUG, logger=_CTRL)
    async with _Queue(entity, lambda _n: ServiceValidationError("x")) as queue:
        await queue.until_calls(5)
        entity.bt_target_temp = 23.5
        queue.request()
        await queue.until_calls(queue.calls + 1)
        queue.request()
        await queue.until_calls(queue.calls + 1)

    records = [
        r for r in caplog.records if r.name == _CTRL and r.levelno >= logging.WARNING
    ]
    with_traceback = [r for r in records if r.exc_info]
    assert len(records) == queue.calls
    assert [r.levelno for r in with_traceback] == [logging.ERROR, logging.ERROR]
    assert records[0].exc_info and records[5].exc_info
    assert all(_TRV in r.getMessage() for r in records)


@pytest.mark.asyncio
async def test_a_pending_retry_ends_with_the_queue():
    """Stopping the queue drops the retry it had scheduled."""
    entity = _make_self()
    async with _Queue(entity, lambda _n: ServiceValidationError("x")) as queue:
        queue.sleep.hold = True
        await queue.until_calls(1)
    queue.sleep.release()
    for _ in range(10):
        await _REAL_SLEEP(0)

    assert entity.control_queue_task.empty()
