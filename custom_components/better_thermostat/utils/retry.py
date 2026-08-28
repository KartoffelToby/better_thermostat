"""Retry utility for Better Thermostat."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import functools
import logging
import random
from typing import ParamSpec, TypeVar

import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")

# Failures that repeating the call cannot fix: they report a defect in this
# integration or in the payload it hands to a service, not a device or a bus
# that is momentarily out of reach. They surface on the first attempt instead
# of being hidden behind the full backoff budget.
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
)


def async_retry(
    retries: int = 1,
    base_delay: float = 1.0,
    jitter: float = 0.2,
    backoff_factor: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
    log_level: int = logging.ERROR,
    identifier: str = "",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Retry async functions when exceptions occur.

    Exceptions in :data:`UNRECOVERABLE_EXCEPTIONS` are re-raised on the first
    attempt even when ``exceptions`` covers them, so a broken call fails fast
    with its own traceback rather than after the whole backoff budget.

    Args:
        retries: Number of retries before giving up
        base_delay: Initial delay between retries in seconds
        jitter: Random jitter factor as a percentage (0.2 = 20% variation)
        backoff_factor: Exponential backoff multiplier (2.0 = double the delay each retry)
        max_delay: Maximum delay in seconds, regardless of backoff calculation
        exceptions: Tuple of exceptions to catch and retry on
        log_level: Logging level for retry attempts (e.g. logging.WARNING, logging.ERROR)
        identifier: Optional identifier string to include in log messages
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
                    if isinstance(e, UNRECOVERABLE_EXCEPTIONS):
                        log_message = (
                            f"{log_prefix}{func.__name__} hit an error that "
                            f"retrying cannot fix: {e}{entity_suffix}"
                        )
                        _LOGGER.exception(log_message)
                        raise

                    if attempt >= retries:
                        log_message = (
                            f"{log_prefix}{func.__name__} failed after "
                            f"{retries + 1} attempts: {e}{entity_suffix}"
                        )
                        _LOGGER.exception(log_message)
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

                    _LOGGER.log(log_level, log_message, exc_info=True)

                    await asyncio.sleep(actual_delay)
                    attempt += 1

        return wrapper

    return decorator
