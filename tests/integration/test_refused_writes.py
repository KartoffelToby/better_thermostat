"""A TRV that refuses a setpoint for good is not rewritten at the cycle rate.

Home Assistant answers a payload a device cannot take (a setpoint outside its
range, for instance) with ``ServiceValidationError`` on every attempt. The
control cycle that sends it fails every time, and these tests pin how often
it comes back, what the log carries about it, and that a target the device
does take still reaches it.
"""

import asyncio
from datetime import timedelta
import logging
from unittest.mock import patch

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.better_thermostat.utils import controlling, retry
from custom_components.better_thermostat.utils.controlling import (
    FAILED_CYCLE_BACKOFF_MAX_S,
    FAILED_CYCLE_BACKOFF_S,
)

from .conftest import (
    BT_ENTITY,
    make_entry,
    set_room_sensor,
    setup_entry,
    wait_for,
    wait_for_startup,
)

# The TRV advertises a wider range than it takes: every setpoint above this is
# refused. A room target of 23.5 °C reaches it above that line once the
# calibration has added its offset.
_DEVICE_TAKES_UP_TO = 24.0
_REFUSED_TARGET = 23.5
_TAKEN_TARGET = 20.0


class _VirtualTime:
    """A clock that advances by every pause the integration asks for.

    The pauses themselves return at once, so a run of retries that spans
    hours of device time passes in a moment, and the entity's write budget
    reads the same clock.
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


class _RoundLog(logging.Handler):
    """Collect what the control loop logs, stamped with virtual time."""

    def __init__(self, clock: _VirtualTime) -> None:
        super().__init__(logging.DEBUG)
        self.clock = clock
        self.rounds: list[tuple[float, logging.LogRecord]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.levelno >= logging.WARNING
            and "controlling TRV" in record.getMessage()
        ):
            self.rounds.append((self.clock.t, record))


@pytest.fixture
async def refusing_room(hass, fake_trv, request):
    """A running room whose TRV refuses every setpoint above a line.

    Parametrize indirectly with ``"numbered"`` for a refusal whose message
    carries a running sequence number, the way a radio stack reports a
    timeout.
    """
    numbered = getattr(request, "param", "fixed") == "numbered"
    refusals = [0]
    set_room_sensor(hass, 18.0)
    entry = make_entry(fake_trv.profile)
    await setup_entry(hass, entry)
    bt = await wait_for_startup(hass, entry)
    await hass.async_block_till_done()

    clock = _VirtualTime(bt.clock)
    real_sleep = asyncio.sleep

    async def virtual_sleep(delay, result=None, **kwargs):
        if delay and delay > 0:
            clock.t += delay
        await real_sleep(0.001 if delay and delay > 0 else 0)
        return result

    original = type(fake_trv).async_set_temperature

    async def refuse_high_setpoints(device, **kwargs):
        temperature = kwargs.get("temperature")
        if (
            device is fake_trv
            and temperature is not None
            and temperature > _DEVICE_TAKES_UP_TO
        ):
            fake_trv.set_temperature_calls.append(temperature)
            refusals[0] += 1
            if numbered:
                raise HomeAssistantError(f"timeout (tsn {refusals[0]})")
            raise ServiceValidationError("temperature out of range")
        return await original(device, **kwargs)

    log = _RoundLog(clock)
    logger = logging.getLogger(controlling.__name__)
    logger.addHandler(log)
    try:
        with (
            patch("asyncio.sleep", new=virtual_sleep),
            patch.object(
                type(fake_trv), "async_set_temperature", refuse_high_setpoints
            ),
            # The retry decorator's jitter would move the budget window from
            # run to run; without it every run of attempts spans the same time.
            patch.object(retry.random, "uniform", return_value=0.0),
        ):
            bt.clock = clock
            yield bt, fake_trv, log
    finally:
        logger.removeHandler(log)


async def _set_target(hass, temperature: float) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        "set_temperature",
        {"entity_id": BT_ENTITY, "temperature": temperature},
        blocking=True,
    )


@pytest.mark.parametrize("refusing_room", ["fixed", "numbered"], indirect=True)
async def test_a_refused_setpoint_is_retried_at_a_growing_distance(hass, refusing_room):
    """Each failed cycle waits twice as long as the one before, up to the cap."""
    _bt, _trv, log = refusing_room
    await _set_target(hass, _REFUSED_TARGET)

    assert await wait_for(hass, lambda: len(log.rounds) >= 11, timeout_s=20.0)

    times = [t for t, _ in log.rounds[:11]]
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    expected = [
        min(FAILED_CYCLE_BACKOFF_S * 2**i, FAILED_CYCLE_BACKOFF_MAX_S)
        for i in range(len(gaps))
    ]
    assert all(gap >= wait for gap, wait in zip(gaps, expected, strict=True)), gaps
    assert gaps[-1] >= FAILED_CYCLE_BACKOFF_MAX_S


@pytest.mark.parametrize("refusing_room", ["fixed", "numbered"], indirect=True)
async def test_a_refused_setpoint_logs_its_traceback_once(hass, refusing_room):
    """The first failure carries the traceback, every later one a single line."""
    _bt, _trv, log = refusing_room
    await _set_target(hass, _REFUSED_TARGET)

    assert await wait_for(hass, lambda: len(log.rounds) >= 6, timeout_s=20.0)

    records = [record for _, record in log.rounds]
    assert [bool(r.exc_info) for r in records[:6]] == [True] + [False] * 5
    assert records[0].levelno == logging.ERROR
    assert {r.levelno for r in records[1:6]} == {logging.WARNING}


async def test_a_target_the_trv_takes_reaches_it_after_a_refused_one(
    hass, refusing_room
):
    """A long run of refusals does not keep a new target from the device."""
    bt, trv, log = refusing_room
    await _set_target(hass, _REFUSED_TARGET)
    assert await wait_for(hass, lambda: len(log.rounds) >= 8, timeout_s=20.0)

    refused_rounds = len(log.rounds)

    await _set_target(hass, _TAKEN_TARGET)

    assert await wait_for(
        hass,
        lambda: (trv.target_temperature or 99.0) <= _DEVICE_TAKES_UP_TO,
        timeout_s=20.0,
    ), trv.set_temperature_calls[-5:]
    assert bt.bt_target_temp == _TAKEN_TARGET
    assert len(log.rounds) == refused_rounds
