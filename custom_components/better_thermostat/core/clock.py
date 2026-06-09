"""Time access for the functional core.

All time-dependent logic receives a :class:`Clock` instead of calling
``time.monotonic()`` or Home Assistant's ``dt_util`` directly, so that
tests and offline replay can drive time deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of monotonic and wall-clock time."""

    def monotonic(self) -> float:
        """Return a monotonically increasing timestamp in seconds."""
        ...

    def now(self) -> datetime:
        """Return the current wall-clock time as an aware local datetime."""
        ...

    def utcnow(self) -> datetime:
        """Return the current wall-clock time as an aware UTC datetime."""
        ...


def _default_now() -> datetime:
    """Return the FakeClock epoch (an arbitrary fixed aware datetime)."""
    return datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    """Deterministic :class:`Clock` for tests and offline replay.

    Both time axes advance together via :meth:`advance`; they never move
    on their own.
    """

    monotonic_value: float = 0.0
    now_value: datetime = field(default_factory=_default_now)

    def monotonic(self) -> float:
        """Return the controlled monotonic timestamp."""
        return self.monotonic_value

    def now(self) -> datetime:
        """Return the controlled wall-clock time."""
        return self.now_value

    def utcnow(self) -> datetime:
        """Return the controlled wall-clock time in UTC."""
        return self.now_value.astimezone(UTC)

    def advance(self, seconds: float) -> None:
        """Move both time axes forward by ``seconds``."""
        self.monotonic_value += seconds
        self.now_value += timedelta(seconds=seconds)
