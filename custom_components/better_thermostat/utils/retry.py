"""Retry utility for Better Thermostat."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager, contextmanager
import functools
import logging
import random
from typing import ParamSpec, TypeVar

from homeassistant.exceptions import ServiceNotFound, ServiceValidationError
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# Failures that repeating the call cannot fix: they report a defect in this
# integration or in the payload it hands to a service, not a device or a bus
# that is momentarily out of reach. They surface on the first attempt instead
# of being hidden behind the full backoff budget. ``ServiceValidationError``
# is Home Assistant refusing the payload itself (a setpoint outside the
# entity's range, a mode it does not offer), which the same payload meets
# again on every attempt.
UNRECOVERABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    AttributeError,
    ImportError,
    IndexError,
    KeyError,
    NameError,
    NotImplementedError,
    TypeError,
    ZeroDivisionError,
    vol.Invalid,
    ServiceValidationError,
)

# Unrecoverable by type, yet momentary: Home Assistant raises
# ``ServiceNotFound`` for a service whose integration is still loading or
# reloading, and the service is back a few seconds later.
RETRYABLE_DESPITE_TYPE: tuple[type[Exception], ...] = (ServiceNotFound,)


class CommandCancelledError(ConnectionError):
    """A device command its client library cancelled while it was in flight."""


@contextmanager
def command_cancellation_as_disconnect() -> Generator[None]:
    """Turn a device command its library cancelled into a lost connection.

    The Z-Wave JS and Matter clients cancel the future of every command still
    in flight when their connection drops, so the service call waiting on it
    raises ``asyncio.CancelledError`` in a task nobody cancelled. Left as it
    is, that cancellation ends whatever task made the write, the control loop
    included, and slips past every handler that catches device failures. A
    cancellation of the current task itself is passed on unchanged.

    Raises
    ------
    CommandCancelledError
        When the command was cancelled while the current task was not
    """
    try:
        yield
    except asyncio.CancelledError as err:
        task = asyncio.current_task()
        if task is None or task.cancelling():
            raise
        raise CommandCancelledError(
            "the device's client library cancelled the command"
        ) from err


# How long one call to a device may take before it counts as failed. Home
# Assistant puts no bound on a service call, and an integration whose call
# waits on a device that never answers (a sleeping Z-Wave node, a cloud API
# without a request timeout) would otherwise keep the caller waiting forever.
DEVICE_CALL_TIMEOUT_S = 30.0


class DeviceCallTimeoutError(TimeoutError):
    """A device call that did not return within ``DEVICE_CALL_TIMEOUT_S``."""


@asynccontextmanager
async def device_call_deadline() -> AsyncGenerator[None]:
    """Bound the device call in the block to ``DEVICE_CALL_TIMEOUT_S``.

    A call still running at the deadline is cancelled and raises
    :class:`DeviceCallTimeoutError`, which the caller handles like any other
    device failure. A ``TimeoutError`` the call raises on its own before the
    deadline is passed on unchanged.

    Raises
    ------
    DeviceCallTimeoutError
        When the call in the block did not return in time
    """
    deadline = asyncio.timeout(DEVICE_CALL_TIMEOUT_S)
    try:
        async with deadline:
            yield
    except TimeoutError as err:
        if not deadline.expired():
            raise
        raise DeviceCallTimeoutError(
            f"the device did not answer within {DEVICE_CALL_TIMEOUT_S:g} s"
        ) from err


def async_retry(
    retries: int = 1,
    base_delay: float = 1.0,
    jitter: float = 0.2,
    backoff_factor: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    log_level: int = logging.DEBUG,
    identifier: str = "",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Retry async functions when exceptions occur.

    Exceptions in :data:`UNRECOVERABLE_EXCEPTIONS`, other than those in
    :data:`RETRYABLE_DESPITE_TYPE`, are re-raised on the first
    attempt even when ``exceptions`` covers them, so a broken call fails fast
    rather than after the whole backoff budget. A :class:`DeviceCallTimeoutError`
    is re-raised on the first attempt as well: the call has already waited
    out its deadline, and another attempt at a device that does not answer
    only repeats the wait. An attempt that is retried
    is logged at ``log_level``, debug unless the caller asks otherwise, and
    the failure that ends the attempts as one warning. The traceback goes
    with them only while debug logging is on, and the caller the error is
    handed back to reports it as loudly as it needs.

    Parameters
    ----------
    retries : int
        number of retries before giving up
    base_delay : float
        initial delay between retries, in seconds
    jitter : float
        random jitter as a fraction of the delay (0.2 = 20 % variation)
    backoff_factor : float
        exponential backoff multiplier (2.0 doubles the delay each retry)
    max_delay : float
        ceiling on the delay in seconds, whatever the backoff computes
    exceptions : tuple[type[Exception], ...]
        exception types to catch and retry on
    log_level : int
        logging level for the retry lines, e.g. ``logging.WARNING``
    identifier : str
        optional label included in the log messages

    Returns
    -------
    Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]
        a decorator wrapping an async function in the retry loop
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # The entity id only labels the log line. It is read from the
            # keyword argument, or from the second positional argument that the
            # ``(self, entity_id, ...)`` helpers carry it in. A signature of a
            # different shape leaves the line without an entity rather than
            # naming an unrelated argument as one.
            entity_id = kwargs.get("entity_id")
            if entity_id is None and len(args) > 1:
                entity_id = args[1]
            if not isinstance(entity_id, str):
                entity_id = None

            log_prefix = f"better_thermostat{f' {identifier}' if identifier else ''}: "
            entity_suffix = f" to entity {entity_id}" if entity_id else ""

            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    if isinstance(e, DeviceCallTimeoutError):
                        raise
                    if isinstance(e, UNRECOVERABLE_EXCEPTIONS) and not isinstance(
                        e, RETRYABLE_DESPITE_TYPE
                    ):
                        log_message = (
                            f"{log_prefix}{func.__name__} hit an error that "
                            f"retrying cannot fix: {e}{entity_suffix}"
                        )
                        _LOGGER.warning(
                            log_message, exc_info=_LOGGER.isEnabledFor(logging.DEBUG)
                        )
                        raise

                    if attempt >= retries:
                        log_message = (
                            f"{log_prefix}{func.__name__} failed after "
                            f"{retries + 1} attempts: {e}{entity_suffix}"
                        )
                        _LOGGER.warning(
                            log_message, exc_info=_LOGGER.isEnabledFor(logging.DEBUG)
                        )
                        raise

                    # Calculate exponential backoff
                    delay = min(base_delay * (backoff_factor**attempt), max_delay)

                    # Apply jitter
                    jitter_range = delay * jitter
                    actual_delay = max(
                        0.1, delay + random.uniform(-jitter_range, jitter_range)
                    )

                    log_message = (
                        f"{log_prefix}{func.__name__} attempt {attempt + 1}/{retries + 1} "
                        f"failed: {e}{entity_suffix}, retrying in {actual_delay:.2f}s"
                    )

                    _LOGGER.log(
                        log_level,
                        log_message,
                        exc_info=_LOGGER.isEnabledFor(logging.DEBUG),
                    )

                    await asyncio.sleep(actual_delay)
                    attempt += 1

        return wrapper

    return decorator
