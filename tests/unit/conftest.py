"""Shared fixtures for the unit layer."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

# Wall-clock seconds per second the code under test asks to sleep.
SLEEP_SCALE = 0.001


@pytest.fixture(autouse=True)
def _scaled_sleeps():
    """Run asyncio sleeps at a thousandth of their requested length.

    The control path waits real intervals: a settle pause after each TRV
    write and one-second polls while a written mode or setpoint is
    confirmed. Scaling every delay by the same factor keeps the order in
    which concurrent sleepers wake, which the lock tests depend on, and a
    zero delay still yields to the event loop exactly once. A test that
    patches ``asyncio.sleep`` itself replaces this for its own scope.
    """
    real_sleep = asyncio.sleep

    async def scaled(delay, result=None):
        return await real_sleep(delay * SLEEP_SCALE if delay > 0 else 0, result)

    with patch("asyncio.sleep", new=scaled):
        yield
