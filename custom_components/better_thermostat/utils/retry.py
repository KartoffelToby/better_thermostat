"""Retry utility for Better Thermostat."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import functools
import logging
import random
from typing import ParamSpec, TypeVar

_LOGGER = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


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
            # Extract entity_id from args/kwargs if available for better logging
            entity_id = kwargs.get("entity_id")
            if (
                entity_id is None and len(args) > 1
            ):  # Assuming self and entity_id are first two args
                entity_id = args[1]

            log_prefix = f"better_thermostat{f' {identifier}' if identifier else ''}: "
            entity_suffix = f" to entity {entity_id}" if entity_id else ""

            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
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
