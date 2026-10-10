"""Halt a control cycle inside a device write, and let the test act meanwhile.

A control cycle stands the inbound event handler down for as long as it runs,
and several watchdogs keep a channel closed until the device echoes a command.
What a device reports inside those windows only exists as a race in
production, and in the harness it does not exist at all: the simulated devices
confirm every write the instant it arrives and the sleeps are compressed, so a
cycle is over before a test gets to say anything.

``holding_next_write`` opens that window on purpose. The device applies the
next write of the named kind and publishes it, then keeps the service call
open until the test releases it, the way a radio integration keeps the call
open until the device acknowledges. Better Thermostat's cycle waits on that
call, so everything the test does before the release lands inside the cycle.

``deferring_next_write`` is the slow device instead: the service call returns
at once, and the device keeps reporting what it held before until the test
lets the command land. That is a battery device waiting for its wake-up, or a
cloud integration waiting for its next poll, and it is what keeps a watchdog
open for as long as the test needs.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from typing import Literal

from .conftest import SimulatedClimate, SimulatedOffsetNumber

# How long a device has to confirm a write before Better Thermostat gives up
# on it. A test that asks whether a watchdog ends because the device answered
# raises it out of reach, so that giving up cannot pass for an answer.
CONFIRM_TIMEOUT = (
    "custom_components.better_thermostat.utils.controlling.WRITE_CONFIRM_TIMEOUT_S"
)

# The service methods a simulated device receives writes through.
WriteMethod = Literal[
    "async_set_temperature", "async_set_hvac_mode", "async_set_native_value"
]


async def poll_until(hass, predicate, timeout_seconds: float = 10.0) -> bool:
    """Yield to the loop until ``predicate()`` is true or time runs out.

    The counterpart of ``conftest.wait_for`` for the time a write is held:
    that one blocks until Home Assistant has finished every task it tracks,
    and the held service call is one of them, so it would wait for a release
    that only the waiting test can give. It is also the one to bound a wait
    with: ``timeout_seconds`` is all it waits, however busy the loop is.
    """
    deadline = hass.loop.time() + timeout_seconds
    while hass.loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


@dataclass
class DeferredWrite:
    """One write the device has accepted and not yet applied."""

    apply: Callable[[], Awaitable[None]] | None = None
    arguments: tuple[object, ...] = ()
    keyword_arguments: dict[str, object] = field(default_factory=dict)

    async def land(self) -> None:
        """Apply the write on the device now, and publish it."""
        assert self.apply is not None, "the device never received the write"
        apply, self.apply = self.apply, None
        await apply()


@dataclass
class HeldWrite:
    """One write the device has applied and not yet answered."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    arguments: tuple[object, ...] = ()
    keyword_arguments: dict[str, object] = field(default_factory=dict)

    async def wait_reached(self, hass) -> None:
        """Return once the write has arrived and been applied, or fail."""
        assert await poll_until(hass, self.reached.is_set), (
            "the device never received the write that was to be held"
        )

    def release(self) -> None:
        """Answer the write, letting the cycle that sent it carry on."""
        self.released.set()


@contextlib.asynccontextmanager
async def holding_next_write(
    device: SimulatedClimate | SimulatedOffsetNumber, method: WriteMethod
) -> AsyncIterator[HeldWrite]:
    """Hold the next write ``device`` receives through ``method``.

    Only the first write is held; every later one, including a press the test
    sends through the same service while the first is held, passes straight
    through. Leaving the block releases a write that is still held, so a
    failing test cannot leave a cycle hanging behind it.
    """
    original = getattr(device, method)
    held = HeldWrite()

    async def holding(*args, **kwargs):
        if held.reached.is_set():
            return await original(*args, **kwargs)
        held.arguments = args
        held.keyword_arguments = kwargs
        await original(*args, **kwargs)
        held.reached.set()
        await held.released.wait()
        return None

    # An instance attribute shadows the class method the service handler
    # looks up, and deleting it restores that method.
    setattr(device, method, holding)
    try:
        yield held
    finally:
        held.release()
        delattr(device, method)


@contextlib.asynccontextmanager
async def deferring_next_write(
    device: SimulatedClimate | SimulatedOffsetNumber, method: WriteMethod
) -> AsyncIterator[DeferredWrite]:
    """Accept the next write ``device`` receives through ``method``, apply it later.

    The service call returns at once without touching the device, and the
    write lands when the test calls ``land()``. Only the first write is
    deferred; later ones pass straight through, so a newer command can
    overtake the deferred one exactly as it does on a slow device.
    """
    original = getattr(device, method)
    deferred = DeferredWrite()
    taken = False

    async def deferring(*args, **kwargs):
        nonlocal taken
        if taken:
            return await original(*args, **kwargs)
        taken = True
        deferred.arguments = args
        deferred.keyword_arguments = kwargs

        async def apply() -> None:
            await original(*args, **kwargs)

        deferred.apply = apply
        return None

    setattr(device, method, deferring)
    try:
        yield deferred
    finally:
        delattr(device, method)
