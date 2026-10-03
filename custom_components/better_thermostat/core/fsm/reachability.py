"""Reachability region (per TRV): online <-> offline with retry backoff.

The region debounces nothing — addressing skips an unavailable TRV
directly via the snapshot. What the region carries is the typed record
of *since when* a TRV is offline and how many retries have elapsed,
recorded with every decision tuple for outage diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

RETRY_INITIAL_S = 30.0
RETRY_MAX_S = 600.0

# Exponent at which the backoff already saturates at RETRY_MAX_S. Clamping the
# exponent to this before ``2.0**`` keeps long outages from overflowing the
# float power before the ``min`` clamp would cap it.
_MAX_BACKOFF_EXP = math.ceil(math.log2(RETRY_MAX_S / RETRY_INITIAL_S))


@dataclass(frozen=True)
class ReachabilityState:
    """State of one TRV's reachability region."""

    online: bool = True
    offline_since: float | None = None
    retry_count: int = 0
    retry_at: float | None = None


def _backoff(retry_count: int) -> float:
    """Return the retry interval in seconds for ``retry_count`` elapsed retries.

    Parameters
    ----------
    retry_count : int
        Number of retries already elapsed while offline.

    Returns
    -------
    float
        Exponential backoff (30 s doubling) capped at ``RETRY_MAX_S``.
    """
    if retry_count <= 0:
        return RETRY_INITIAL_S
    clamped = min(retry_count, _MAX_BACKOFF_EXP)
    return min(RETRY_MAX_S, RETRY_INITIAL_S * (2.0**clamped))


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
