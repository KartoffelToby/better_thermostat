"""A control cycle that keeps failing is retried at a growing distance.

A device that refuses a payload for good (a setpoint outside its range, a mode
it does not offer) fails every cycle that sends it. The control queue retries
such a cycle on its own, and these tests pin how far apart those retries land,
when a run of failures ends, what the log says about it, and that a request
arriving in the meantime is not held back by the pause.
"""

import asyncio
from collections.abc import Awaitable
from contextlib import ExitStack
import logging
from typing import Protocol
from unittest.mock import AsyncMock, patch

from homeassistant.components.climate.const import PRESET_BOOST, HVACMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.core.clock import FakeClock
from custom_components.better_thermostat.core.decide import decide
from custom_components.better_thermostat.trv import Trv
from custom_components.better_thermostat.utils.controlling import (
    FAILED_CYCLE_BACKOFF_MAX_S,
    FAILED_CYCLE_BACKOFF_S,
    MIN_WRITE_INTERVAL_S,
    control_queue,
)
from custom_components.better_thermostat.utils.snapshot import _build_trv_reported
from tests.factories import ThermostatStandIn, make_snapshot, make_state

_CTRL = "custom_components.better_thermostat.utils.controlling"
_QUIRKS = "custom_components.better_thermostat.model_fixes.model_quirks"
_SNAPSHOT = "custom_components.better_thermostat.utils.snapshot"
_TRV = "climate.trv1"

_REAL_SLEEP = asyncio.sleep


class _VirtualSleep:
    """Stand-in for ``asyncio.sleep`` that records every pause it is asked for.

    A pause returns at once unless ``hold`` is set, in which case a pause of a
    second or more waits until ``release`` is called. Holding lets a test act
    while the queue's retry is still pending. With a ``clock`` set, a pause of
    a second or more moves that clock on by its length.
    """

    def __init__(self) -> None:
        self.waits: list[float] = []
        self.hold = False
        self.clock: FakeClock | None = None
        self._released = asyncio.Event()

    def release(self) -> None:
        self.hold = False
        self._released.set()

    async def __call__(self, delay, result=None):
        if self.clock is not None and delay and delay >= 1:
            self.clock.advance(delay)
        if delay and delay > 0:
            self.waits.append(delay)
            if self.hold and delay >= 1:
                await self._released.wait()
        await _REAL_SLEEP(0)
        return result


def _make_self() -> ThermostatStandIn:
    entity = ThermostatStandIn()
    entity.device_name = "room"
    entity.in_maintenance = False
    entity.ignore_states = False
    entity.startup_running = False
    entity.calculate_heating_power = AsyncMock()
    entity.calculate_heat_loss = AsyncMock()
    entity.cooler_entity_id = None
    entity.real_trvs = {_TRV: Trv(entity_id=_TRV)}
    entity.heat_target_temperature = 21.0
    entity.cool_target_temperature = None
    entity.bt_hvac_mode = HVACMode.HEAT
    entity.clock = FakeClock()
    # The TRV is present, so a cycle that reports it clean did control it.
    entity.hass.states.get.return_value = State(_TRV, "heat")
    entity.control_queue_task = asyncio.Queue(maxsize=1)
    return entity


def _refused(*_args, **_kwargs):
    raise ServiceValidationError("out of range")


class _ControlTrv(Protocol):
    """The shape of ``control_trv`` as the control queue calls it."""

    def __call__(
        self, entity: object, entity_id: str, /, cycle: object = None
    ) -> Awaitable[object]: ...


class _Queue:
    """Run the control queue of one entity against a scripted control_trv."""

    def __init__(
        self,
        entity: ThermostatStandIn,
        outcomes,
        cycle=None,
        control_trv: _ControlTrv | None = None,
    ) -> None:
        self.entity = entity
        self.outcomes = outcomes
        self.cycle = cycle
        self.calls = 0
        self.sleep = _VirtualSleep()
        self.control_trv: _ControlTrv = (
            control_trv if control_trv is not None else self._control_trv
        )
        self._task: asyncio.Task[None] | None = None
        self._patches = ExitStack()

    async def _control_trv(self, _entity, _entity_id, cycle=None):
        outcome = self.outcomes(self.calls)
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _compute_cycle(self, entity, *_args, **_kwargs):
        return None if self.cycle is None else self.cycle(entity)

    async def __aenter__(self):
        self._patches.enter_context(patch(f"{_CTRL}.control_trv", new=self.control_trv))
        self._patches.enter_context(
            patch(f"{_CTRL}.compute_control_cycle", side_effect=self._compute_cycle)
        )
        self._patches.enter_context(patch("asyncio.sleep", new=self.sleep))
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
        self._patches.close()

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
        ("heat_target_temperature", 23.5),
        ("cool_target_temperature", 26.0),
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
        trv.commanded_setpoint = 22.0 + 0.1 * n
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
    entity.real_trvs[other] = Trv(entity_id=other)
    caplog.set_level(logging.DEBUG, logger=_CTRL)
    rounds = []

    async def control_trv(_entity, entity_id, cycle=None):
        rounds.append(entity_id)
        failing = _TRV if (len(rounds) - 1) // 2 % 2 == 0 else other
        if entity_id == failing:
            raise HomeAssistantError(f"no answer from {entity_id}")
        return True

    queue = _Queue(entity, None, control_trv=control_trv)
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
        entity.heat_target_temperature = 23.5
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
        entity.heat_target_temperature = 23.5
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


@pytest.mark.asyncio
async def test_a_run_at_its_longest_pause_warns_once_an_hour(caplog):
    """A device that keeps refusing is reported hourly once the pause is capped.

    Below the cap each failure is a warning of its own. At the cap the run
    has settled into one attempt every few minutes, and a warning for each
    of those would fill the log with the same line all day.
    """
    entity = _make_self()
    caplog.set_level(logging.DEBUG, logger=_CTRL)
    queue = _Queue(entity, lambda _n: HomeAssistantError("no answer"))
    queue.sleep.clock = entity.clock
    async with queue:
        # The ninth failure reaches the cap; 36 more span three hours there.
        await queue.until_calls(45, timeout=10.0)

    lines = [
        r
        for r in caplog.records
        if r.name == _CTRL and "controlling TRV" in r.getMessage()
    ]
    warnings = [r for r in lines if r.levelno == logging.WARNING]
    below_cap = [r for r in warnings if "failed again" in r.getMessage()]
    at_cap = [r for r in warnings if "still failing" in r.getMessage()]
    assert len(below_cap) == 7
    assert 3 <= len(at_cap) <= 4
    assert all("min" in r.getMessage() for r in at_cap)
    assert len(lines) >= 45


@pytest.mark.asyncio
async def test_a_cycle_that_skips_the_failing_trv_keeps_the_run():
    """A cycle that finds the failing TRV unavailable does not end the run.

    The cycle leaves such a device out and reports nothing wrong, but it never
    tried the refused write, so it says nothing about whether the device
    takes it now; the next failure continues the run.
    """
    entity = _make_self()
    present = State(_TRV, "heat")
    gone = State(_TRV, STATE_UNAVAILABLE)
    script = {0: "refused", 1: "skipped", 2: "refused"}

    def outcome(n):
        step = script.get(n, "refused")
        entity.hass.states.get.return_value = gone if step == "skipped" else present
        return True if step == "skipped" else HomeAssistantError("no answer")

    async with _Queue(entity, outcome) as queue:
        await queue.until_calls(2)
        # The device comes back, and its report asks for a cycle.
        queue.request()
        await queue.until_calls(3)

    assert queue.sleep.waits[:2] == [2.0, 4.0]


@pytest.mark.asyncio
async def test_a_trv_still_away_since_it_failed_keeps_the_run():
    """A TRV that failed earlier in the run and has not been back keeps it going.

    The other TRV's clean write says nothing about the one that has been
    unavailable since it failed.
    """
    other = "climate.trv2"
    entity = _make_self()
    entity.real_trvs[other] = Trv(entity_id=other)
    states = {_TRV: State(_TRV, "heat"), other: State(other, "heat")}
    entity.hass.states.get.side_effect = states.get
    rounds = []
    # Per round: which TRVs fail and whether the other one is away.
    script = [({_TRV, other}, False), ({_TRV}, True), (set(), True), ({other}, False)]

    async def control_trv(_entity, entity_id, cycle=None):
        rounds.append(entity_id)
        failing, away = script[min((len(rounds) - 1) // 2, len(script) - 1)]
        states[other] = State(other, STATE_UNAVAILABLE if away else "heat")
        if entity_id == other and away:
            return True
        if entity_id in failing:
            raise HomeAssistantError(f"no answer from {entity_id}")
        return True

    queue = _Queue(entity, None, control_trv=control_trv)
    async with queue:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while len(rounds) < 6 and loop.time() < deadline:
            await _REAL_SLEEP(0)
        for _ in range(5):
            await _REAL_SLEEP(0)
        queue.request()
        while len(rounds) < 8 and loop.time() < deadline:
            await _REAL_SLEEP(0)
        for _ in range(5):
            await _REAL_SLEEP(0)

    assert queue.sleep.waits[:3] == [2.0, 4.0, 8.0]


def _decided_cycle(entity):
    """Observe the TRV as the snapshot does and run the kernel's decision on it."""
    snapshot = make_snapshot(
        heat_target_temperature=entity.heat_target_temperature,
        room_temperature=18.0,
        preset_mode=entity.preset_mode,
        trvs={_TRV: _build_trv_reported(entity, _TRV, entity.real_trvs[_TRV])},
    )
    desired, _ = decide(snapshot, make_state())
    return snapshot, desired


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "unknown_as_available", "boost", "cycle", "next_wait"),
    [
        # A device whose quirk runs it while its entity reports unknown is
        # addressed in that state, so a clean cycle there ends the run.
        (STATE_UNKNOWN, True, False, _decided_cycle, FAILED_CYCLE_BACKOFF_S),
        # Without that quirk an unknown device is not addressed and keeps it.
        (STATE_UNKNOWN, False, False, _decided_cycle, 8.0),
        # A boost that is heating addresses an unavailable device as well.
        (STATE_UNAVAILABLE, False, True, _decided_cycle, FAILED_CYCLE_BACKOFF_S),
        # Without a shared decision the quirk still reads unknown as present.
        (STATE_UNKNOWN, True, False, None, FAILED_CYCLE_BACKOFF_S),
    ],
)
async def test_a_clean_cycle_ends_the_run_where_its_decision_addressed_the_trv(
    state, unknown_as_available, boost, cycle, next_wait
):
    """Whether a clean cycle controlled the TRV follows the cycle's own decision.

    The cycle writes to every TRV its decision addresses, so a clean retry on
    such a TRV has tried the refused write again and ends the run.
    """
    entity = _make_self()
    entity.hass.states.get.return_value = State(_TRV, state)
    entity.preset_mode = PRESET_BOOST if boost else None
    script = {0: False, 1: False, 2: True}
    with (
        patch(
            f"{_SNAPSHOT}.trv_state_unknown_as_available",
            return_value=unknown_as_available,
        ),
        patch(
            f"{_QUIRKS}.trv_state_unknown_as_available",
            return_value=unknown_as_available,
        ),
    ):
        async with _Queue(entity, lambda n: script.get(n, False), cycle) as queue:
            await queue.until_calls(3)
            queue.request()
            await queue.until_calls(4)

    assert queue.sleep.waits[:3] == [2.0, 4.0, next_wait]
