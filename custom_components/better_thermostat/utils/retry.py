"""Retry utility for Better Thermostat."""

import asyncio
import functools
import logging
import random
from typing import TypeVar, Any
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def async_retry(
    retries: int = 1,
    base_delay: float = 1.0,
    jitter: float = 0.2,
    backoff_factor: float = 2.0,
    max_delay: float = 60.0,
    exceptions: tuple = (Exception,),
    log_level: str = "exception",
    identifier: str = "",
):
    """Retry async functions when exceptions occur.

    Args:
        retries: Number of retries before giving up
        base_delay: Initial delay between retries in seconds
        jitter: Random jitter factor as a percentage (0.2 = 20% variation)
        backoff_factor: Exponential backoff multiplier (2.0 = double the delay each retry)
        max_delay: Maximum delay in seconds, regardless of backoff calculation
        exceptions: Tuple of exceptions to catch and retry on
        log_level: Logging level to use ("debug", "info", "warning", "error", "exception")
        identifier: Optional identifier string to include in log messages
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            # Extract entity_id from args/kwargs if available for better logging
            entity_id = kwargs.get("entity_id", None)
            if (
                entity_id is None and len(args) > 2
            ):  # Assuming self and entity_id are first two args
                entity_id = args[1]

            log_prefix = f"better_thermostat{f' {identifier}' if identifier else ''}: "
            entity_suffix = f" to entity {entity_id}" if entity_id else ""

            for attempt in range(retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e

                    # Only log and delay if we're going to retry
                    if attempt < retries:
                        # Calculate exponential backoff
                        delay = min(base_delay * (backoff_factor**attempt), max_delay)

                        # Apply jitter
                        jitter_range = delay * jitter
                        actual_delay = delay + random.uniform(
                            -jitter_range, jitter_range
                        )
                        actual_delay = max(0.1, actual_delay)  # Ensure minimum delay

                        log_message = f"{log_prefix}{func.__name__} attempt {attempt + 1}/{retries + 1} failed: {e}{entity_suffix}, retrying in {actual_delay:.2f}s"

                        if log_level == "debug":
                            _LOGGER.debug(log_message)
                        elif log_level == "info":
                            _LOGGER.info(log_message)
                        elif log_level == "warning":
                            _LOGGER.warning(log_message)
                        elif log_level == "error":
                            _LOGGER.error(log_message)
                        else:  # Default to exception level
                            _LOGGER.exception(log_message)

                        await asyncio.sleep(actual_delay)

            # If we got here, we ran out of retries
            log_message = f"{log_prefix}{func.__name__} failed after {retries + 1} attempts: {last_exception}{entity_suffix}"
            _LOGGER.exception(log_message)
            raise last_exception

        return wrapper

    return decorator
