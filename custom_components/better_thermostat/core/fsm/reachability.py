"""Reachability region (per TRV): online <-> offline with retry backoff.

The region debounces nothing — addressing skips an unavailable TRV
directly via the snapshot. What the region carries is the typed record
of *since when* a TRV is offline and how many retries have elapsed,
recorded with every decision tuple for outage diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass

RETRY_INITIAL_S = 30.0
RETRY_MAX_S = 600.0


@dataclass(frozen=True)
class ReachabilityState:
    """State of one TRV's reachability region."""

    online: bool = True
    offline_since: float | None = None
    retry_count: int = 0
    retry_at: float | None = None


def _backoff(retry_count: int) -> float:
    """Exponential backoff: 30 s doubling up to 10 min."""
    return min(RETRY_MAX_S, RETRY_INITIAL_S * (2.0**retry_count))


def step(
    state: ReachabilityState, reported_available: bool, now: float
) -> ReachabilityState:
    """Advance the region from one availability observation."""
    if reported_available:
        return ReachabilityState()

    if state.online:
        # Fresh transition to offline: schedule the first retry.
        return ReachabilityState(
            online=False, offline_since=now, retry_count=0, retry_at=now + _backoff(0)
        )

    if state.retry_at is not None and now >= state.retry_at:
        # Retry window reached while still offline: back off further.
        retry_count = state.retry_count + 1
        return ReachabilityState(
            online=False,
            offline_since=state.offline_since,
            retry_count=retry_count,
            retry_at=now + _backoff(retry_count),
        )

    return state
