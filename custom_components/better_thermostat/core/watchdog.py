"""Control watchdog: detect the silent hang.

"Controlling worse" is acceptable under degradation; "not controlling at
all without anyone noticing" is not. The watchdog answers one question:
did a control cycle complete recently?
"""

from __future__ import annotations

WATCHDOG_MAX_AGE_S = 900.0


def control_loop_stalled(
    last_control_monotonic: float | None,
    now: float,
    max_age_s: float = WATCHDOG_MAX_AGE_S,
) -> bool:
    """Whether the control loop has silently stalled.

    ``None`` means no cycle has completed yet (startup) and does not
    count as a stall — startup has its own gate.
    """
    if last_control_monotonic is None:
        return False
    return (now - last_control_monotonic) > max_age_s
