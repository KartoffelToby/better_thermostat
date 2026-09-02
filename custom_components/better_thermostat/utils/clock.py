"""System clock implementation backed by Home Assistant time helpers."""

from __future__ import annotations

from datetime import datetime
import time

from homeassistant.util import dt as dt_util


class SystemClock:
    """Wall-clock and monotonic time from the running system.

    Shell-side implementation of :class:`~..core.clock.Clock`.
    """

    def monotonic(self) -> float:
        """Return the system monotonic timestamp."""
        return time.monotonic()

    def now(self) -> datetime:
        """Return the current time in Home Assistant's configured timezone."""
        return dt_util.now()

    def utcnow(self) -> datetime:
        """Return the current time in UTC."""
        return dt_util.utcnow()
