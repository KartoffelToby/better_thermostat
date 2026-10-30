"""Tests for the core Clock protocol and its implementations."""

from datetime import UTC, datetime, timedelta, timezone
import math

import pytest

from custom_components.better_thermostat.core.clock import Clock, FakeClock
from custom_components.better_thermostat.utils.clock import SystemClock


class TestFakeClock:
    """FakeClock drives both time axes deterministically."""

    def test_defaults_are_deterministic(self):
        """Two fresh FakeClocks read identical times."""
        a = FakeClock()
        b = FakeClock()
        assert a.monotonic() == b.monotonic() == 0.0
        assert a.now() == b.now()
        assert a.now().tzinfo is not None

    def test_time_does_not_move_on_its_own(self):
        """Repeated reads return the same instant."""
        clock = FakeClock()
        assert clock.monotonic() == clock.monotonic()
        assert clock.now() == clock.now()

    def test_advance_moves_both_axes(self):
        """advance() shifts monotonic and wall-clock time together."""
        clock = FakeClock()
        start_now = clock.now()
        clock.advance(90.0)
        assert clock.monotonic() == 90.0
        assert clock.now() == start_now + timedelta(seconds=90)

    def test_utcnow_converts_to_utc(self):
        """utcnow() returns the same instant expressed in UTC."""
        cet = timezone(timedelta(hours=1))
        clock = FakeClock(now_value=datetime(2025, 6, 1, 13, 0, 0, tzinfo=cet))
        assert clock.utcnow() == datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        assert clock.utcnow().tzinfo == UTC

    def test_satisfies_clock_protocol(self):
        """FakeClock structurally implements Clock."""
        assert isinstance(FakeClock(), Clock)

    def test_rejects_naive_now_value(self):
        """A naive now_value violates the aware-datetime contract."""
        with pytest.raises(ValueError, match="timezone-aware"):
            FakeClock(now_value=datetime(2025, 1, 1, 12, 0, 0))

    def test_advance_rejects_negative(self):
        """advance() never moves time backwards."""
        clock = FakeClock()
        with pytest.raises(ValueError, match="non-negative"):
            clock.advance(-1.0)

    def test_advance_rejects_non_finite(self):
        """advance() rejects nan/inf that would poison the time axes."""
        clock = FakeClock()
        with pytest.raises(ValueError, match="finite"):
            clock.advance(math.inf)
        with pytest.raises(ValueError, match="finite"):
            clock.advance(math.nan)


class TestSystemClock:
    """SystemClock bridges to real system/Home Assistant time."""

    def test_satisfies_clock_protocol(self):
        """SystemClock structurally implements Clock."""
        assert isinstance(SystemClock(), Clock)

    def test_monotonic_increases(self):
        """Successive monotonic reads never go backwards."""
        clock = SystemClock()
        first = clock.monotonic()
        assert clock.monotonic() >= first

    def test_now_and_utcnow_are_aware(self):
        """Both wall-clock readings carry timezone information."""
        clock = SystemClock()
        assert clock.now().tzinfo is not None
        assert clock.utcnow().tzinfo is not None
